"""Video Translator — web server.

Run:  .venv/bin/python app.py        (PORT env, default 8611)
UI:   http://localhost:8611/
API:  POST /api/jobs {url, mode: subs|dub|both, voice}
      GET  /api/jobs/<id>            job state + step list
      GET  /api/jobs/<id>/download   finished .mp4
      GET  /api/voices               available my-MM voices
"""
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import threading
import time
import uuid
from pathlib import Path

from flask import (Flask, Response, jsonify, request, send_file,
                   send_from_directory)

# CA patch must land before edge_tts/aiohttp are imported anywhere
from pipeline.common import ensure_system_ca

ensure_system_ca()

from pipeline import accounts
from pipeline import download as dl_mod
from pipeline import fx as fx_mod
from pipeline import run_job, tts
from pipeline.common import probe_duration, run as sh

ROOT = Path(__file__).resolve().parent
JOBS_DIR = ROOT / "jobs"
JOBS_DIR.mkdir(exist_ok=True)
PREVIEWS_DIR = ROOT / "previews"
PREVIEWS_DIR.mkdir(exist_ok=True)
UPLOADS_DIR = ROOT / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)

app = Flask(__name__, static_folder=None)
JOBS = {}  # id -> dict (also persisted to jobs/<id>/job.json)

STEP_LABELS = {
    "download": "ဗီဒီယို ဒေါင်းလုပ်",
    "transcribe": "စကားသံကို စာသားထုတ်",
    "translate": "မြန်မာ ဘာသာပြန်",
    "tts": "မြန်မာ အသံထုတ်",
    "retime": "အချိန်ကိုက် ညှိ (လိုရင် ဖရိမ်ရပ်)",
    "finalize": "စာတန်းထိုး (ရုပ်ရှင်ပုံစံ) + Logo/Blur + ဗီဒီယို ပေါင်းစပ်",
}


def _save(job):
    p = JOBS_DIR / job["id"] / "job.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")


class JobCancelled(Exception):
    """Raised inside a worker when the user asked to stop the job."""


def _progress_fn(job):
    def progress(step, state, note=""):
        if job.get("cancel_requested"):
            raise JobCancelled()
        job["updated_at"] = time.time()
        for s in job["steps"]:
            if s["key"] == step:
                s["state"] = state
                if note:
                    s["note"] = note
        job["current"] = step
        _save(job)
    return progress


def _heartbeat(job):
    """Keep updated_at (and the on-disk copy) fresh while a worker is
    alive, so stale-job detection — here and in the launcher gate — can
    tell a genuinely running job from a wedged one."""
    while job.get("status") in ("queued", "running") \
            and not job.get("cancel_requested"):
        job["updated_at"] = time.time()
        try:
            _save(job)
        except Exception:
            pass
        time.sleep(45)


# ---------- auto-cleanup: no job data stays on the server ----------
# 15 minutes after a job finishes (done or error), its whole folder —
# source, intermediates and output — plus the cached preview copy of
# the same URL is deleted, so nothing accumulates on the disk.
CLEANUP_DELAY = 900
CLEANUP_TIMERS = {}


def _job_retention(job):
    """Bot-submitted jobs stay 24h — customers come back for their
    video; web jobs keep the 15-minute hygiene rule."""
    return 24 * 3600 if job.get("tg_chat_id") else CLEANUP_DELAY


def _schedule_cleanup(job):
    jid = job["id"]
    old = CLEANUP_TIMERS.pop(jid, None)
    if old:
        old.cancel()
    delay = _job_retention(job)
    job["cleanup_at"] = time.time() + delay
    t = threading.Timer(delay, _cleanup_job, args=(jid,))
    t.daemon = True
    t.start()
    CLEANUP_TIMERS[jid] = t


# ---------- Telegram round-trip: bot-submitted jobs ----------

_TG_PENDING = []  # job ids created from Telegram, waiting for a free slot
_TG_FLOW = {}     # chat_id -> bot choice flow state (blur spot, mode)
_TG_DAILY_CAP = int(os.environ.get("VT_TG_DAILY_CAP", "30"))
_TG_DAILY_FILE = ROOT / ".tg_daily.json"


def _tg_daily_count(chat_id, add=False):
    """Per-chat bot jobs per day (protects the shared quotas)."""
    today = time.strftime("%Y-%m-%d")
    try:
        store = json.loads(_TG_DAILY_FILE.read_text(encoding="utf-8"))
    except Exception:
        store = {}
    entry = store.get(str(chat_id)) or [today, 0]
    if entry[0] != today:
        entry = [today, 0]
    if add:
        entry[1] += 1
        store[str(chat_id)] = entry
        try:
            _TG_DAILY_FILE.write_text(json.dumps(store),
                                       encoding="utf-8")
        except Exception:
            pass
    return entry[1]


def _backup_loop():
    """Daily snapshot of the money/identity data (users.db, keys,
    recipients) — 14 days kept, private perms. Same-host protection
    against corruption; off-host copies come with the launch setup."""
    time.sleep(90)
    names = ["users.db", ".env", ".tg_recipients.json",
             ".translate_good", ".tg_daily.json"]
    while True:
        try:
            dest = ROOT / "backups" / time.strftime("%Y-%m-%d")
            dest.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(ROOT / "backups", 0o700)
            except Exception:
                pass
            for n in names:
                src = ROOT / n
                if src.exists():
                    shutil.copy2(src, dest / n)
            days = sorted(p.name for p in (ROOT / "backups").iterdir()
                          if p.is_dir())
            for old in days[:-14]:
                shutil.rmtree(ROOT / "backups" / old, ignore_errors=True)
            # Dormant-account sweep (owner rule): accounts with no
            # login for 60 days, zero credits and no jobs ever are
            # deleted (archived in deleted_users). Admin accounts,
            # credit holders and anyone who ever ran a job are
            # untouchable here.
            active = {j.get("user_id") for j in _all_jobs()
                      if j.get("user_id")}
            for u in accounts.dormant_users(60, active):
                accounts.delete_user(u["id"], why="dormant60")
        except Exception:
            pass
        time.sleep(24 * 3600)


def _any_active_worker():
    return any(j.get("worker_started")
               and j.get("status") in ("queued", "running")
               for j in list(JOBS.values()))


def _tg_deliver_async(job):
    """A bot-submitted job finished: send the video back to its chat."""
    chat_id = job.get("tg_chat_id")
    if not chat_id:
        return

    def go():
        from pipeline import telegram_send as tg
        out = JOBS_DIR / job["id"] / "output.mp4"
        try:
            if not out.exists():
                return
            if out.stat().st_size > tg.MAX_BYTES:
                tg.send_message(
                    chat_id,
                    f"✅ ပြီးပါပြီ — ဒါပေမဲ့ ဖိုင် {out.stat().st_size / 1e6:.0f}MB "
                    "ဖြစ်နေလို့ Telegram ကနေ ပို့လို့ မရပါ (50MB အများဆုံး)")
                job["tg"] = {"state": "failed", "to": job.get("tg_from"),
                             "error": "file > 50MB"}
            else:
                job["tg"] = {"state": "sending", "to": job.get("tg_from")}
                _save(job)
                tg.send_video(chat_id, out,
                              caption="🎬 မြန်မာ ဘာသာပြန် ဗီဒီယို\n"
                                      + (job.get("url") or ""))
                job["tg"] = {"state": "sent", "to": job.get("tg_from")}
        except Exception as e:
            job["tg"] = {"state": "failed", "to": job.get("tg_from"),
                         "error": str(e)[:200]}
        _save(job)

    threading.Thread(target=go, daemon=True).start()


def _tg_error_async(job):
    chat_id = job.get("tg_chat_id")
    if not chat_id:
        return

    def go():
        try:
            from pipeline import telegram_send as tg
            tg.send_message(chat_id, "❌ မလုပ်နိုင်ပါ: "
                            + str(job.get("error") or "")[:300])
        except Exception:
            pass

    threading.Thread(target=go, daemon=True).start()


def _on_tg_message(msg):
    """A Telegram user sent something to the bot: a video link becomes
    a job (queued behind whatever is running); anything else gets a
    one-line hint. Private chats only."""
    from pipeline import telegram_send as tg
    chat = msg.get("chat") or {}
    if chat.get("type") != "private":
        return
    chat_id = chat.get("id")
    # A video FILE is a submission too — it may carry no text at all,
    # so it must be handled before the text checks below.
    vid = msg.get("video")
    if not vid:
        doc = msg.get("document") or {}
        if (doc.get("mime_type") or "").startswith("video/"):
            vid = doc
    text = (msg.get("text") or msg.get("caption") or "").strip()
    if _tg_menu_step(msg, chat_id, text):
        return
    if vid:
        if not _may_submit(chat_id):
            _deny_submit(chat_id)
            return
        mine = [j for j in JOBS.values()
                if j.get("tg_chat_id") == chat_id
                and j.get("status") in ("queued", "running")]
        if mine:
            tg.send_message(chat_id, "⏳ သင့် အရင် video မပြီးသေးပါ — "
                                     "ပြီးရင် နောက် တစ်ခု ပို့ပါ")
            return
        if (vid.get("file_size") or 0) > 20 * 1000 * 1000:
            tg.send_message(chat_id, "⚠️ ဒီ file က 20MB ထက် ကြီးနေပါတယ် "
                                     "— link ပို့ပေးပါ")
            return
        frm = msg.get("from") or {}
        _TG_FLOW[chat_id] = {
            "file_id": vid["file_id"], "step": "prepare",
            "from": frm.get("username") or frm.get("first_name"),
            "ts": time.time()}
        tg.send_message(chat_id, "⏳ file ယူနေတယ် — ဝါးမယ့် နေရာ "
                                 "ရွေးဖို့ ပုံ ပို့ပေးမယ်")
        threading.Thread(target=_tg_flow_prepare, args=(chat_id,),
                         daemon=True).start()
        return
    if text.startswith("/"):
        # /start always earns the welcome (it is also the registration
        # moment) and the persistent menu keyboard; other commands
        # stay silent.
        if text.startswith("/start"):
            # Welcome + menu keyboard in ONE call (two calls made
            # the menu visibly trail the greeting).
            tg.send_message(chat_id, tg.WELCOME, keyboard=_MENU_ROWS)
        return
    if not text:
        return
    if not _may_submit(chat_id):
        # Job submission: the fixed admin always; customers only
        # once the owner opens the bot AND their chat is linked.
        _deny_submit(chat_id)
        return
    m = re.search(r"https?://\S+", text)
    if not m:
        tg.send_message(chat_id,
                        "🎬 ဘာသာပြန်ချင်တဲ့ video link ကို ပို့ပါ (YouTube / TikTok)")
        return
    mine = [j for j in JOBS.values()
            if j.get("tg_chat_id") == chat_id
            and j.get("status") in ("queued", "running")]
    if mine:
        tg.send_message(chat_id,
                        "⏳ သင့် အရင် video မပြီးသေးပါ — ပြီးရင် နောက် link ပို့ပါ")
        return
    frm = msg.get("from") or {}
    _TG_FLOW[chat_id] = {
        "url": m.group(0), "step": "prepare",
        "from": frm.get("username") or frm.get("first_name"),
        "ts": time.time()}
    tg.send_message(chat_id,
                    "⏳ video ယူနေတယ် — ဝါးမယ့် နေရာ ရွေးဖို့ ပုံ ပို့ပေးမယ်")
    threading.Thread(target=_tg_flow_prepare, args=(chat_id,),
                     daemon=True).start()


_BLUR_PRESETS = {
    "top": {"x": 0.04, "y": 0.03, "w": 0.92, "h": 0.18},
    "mid": {"x": 0.04, "y": 0.41, "w": 0.92, "h": 0.18},
    "bot": {"x": 0.04, "y": 0.79, "w": 0.92, "h": 0.18},
}

# ---------------- bot account menu: link / balance / top-up --------
_MENU_ROWS = [["🎬 Video ထုတ်မယ်", "💰 Credit ဖြည့်"],
              ["💳 လက်ကျန်", "👤 Username ချိတ်"]]
_LINK_CODES = {}   # code -> (user_id, expires) — web-issued, bot-redeemed
_LINK_FAILS = {}   # chat_id -> (fail_count, locked_until) — code guessing

# Whenever the bot sends a user off to the web app (to register or
# link), the message carries the app link AND the install reminder.
APP_LINK = os.environ.get("VT_APP_URL", "https://85-155-149-58.sslip.io")
_LINK_HINT = ("\n🔗 App: " + APP_LINK +
              "\n📲 တစ်ခါတည်း ဒေါင်းထားပါ: link ကို browser မှာ ဖွင့်ပြီး "
              "menu (⋮) → «Add to Home screen»")


def _pay_info():
    p = ROOT / ".env"
    if p.exists():
        for ln in p.read_text(encoding="utf-8").splitlines():
            if ln.startswith("PAY_INFO="):
                return ln.split("=", 1)[1].strip()
    return ""


def _bot_open():
    """Owner switch: when BOT_OPEN=1 in .env, customers whose
    Telegram chat is linked to a web account may submit bot jobs
    (credit pre-checks + the daily cap apply). Read live on every
    call, so the admin toggle takes effect immediately."""
    p = ROOT / ".env"
    if p.exists():
        for ln in p.read_text(encoding="utf-8").splitlines():
            if ln.startswith("BOT_OPEN="):
                return ln.split("=", 1)[1].strip() == "1"
    return False


def _may_submit(chat_id):
    from pipeline import telegram_send as tg
    if tg.is_admin(chat_id):
        return True
    return _bot_open() and bool(accounts.user_for_tg(chat_id))


def _deny_submit(chat_id):
    from pipeline import telegram_send as tg
    if _bot_open():
        tg.send_message(chat_id, "အရင် 👤 Username ချိတ် (Menu ထဲက) ပါ"
                        + _LINK_HINT)
    else:
        tg.send_message(chat_id, "🔒 အခုတော့ admin အတွက်ပဲ ဖွင့်ထားပါတယ်")


def _tg_menu_step(msg, chat_id, text):
    """Bot account menu + the link / top-up conversations. Returns
    True when the message was consumed by one of them. These are
    ACCOUNT functions (open to anyone who linked an account); job
    submission stays behind the admin lock in the caller."""
    from pipeline import telegram_send as tg
    flow = _TG_FLOW.get(chat_id)
    acct = accounts.user_for_tg(chat_id)
    photos = msg.get("photo")
    if photos and flow and flow.get("step") == "topup_proof" and acct:
        amount = flow.get("amount") or 0
        tid = accounts.create_topup(acct["id"], amount, None)
        tdir = ROOT / "topups"
        tdir.mkdir(exist_ok=True)
        proof = tdir / f"{tid}.jpg"
        ok_file = False
        try:
            tg.download_file(photos[-1]["file_id"], proof)
            accounts.set_topup_proof(tid, proof.name)
            ok_file = True
        except Exception:
            pass
        _TG_FLOW.pop(chat_id, None)
        cap = (f"💰 Credit ဖြည့် တောင်းဆိုချက် #{tid}\n"
               f"👤 {acct['username']} · {amount} credits")
        buttons = [[("✅ Approve", f"t:{tid}:ok"),
                    ("❌ ငြင်း", f"t:{tid}:no")]]
        try:
            if ok_file:
                tg.send_photo(tg.FIXED_ADMIN_CHAT_ID, proof,
                              caption=cap, buttons=buttons)
            else:
                tg.send_message(tg.FIXED_ADMIN_CHAT_ID,
                                cap + "\n(ပုံ ယူမရ)", buttons=buttons)
        except Exception:
            pass
        tg.send_message(chat_id, "✅ တောင်းဆိုချက် admin ဆီ ပို့ပြီ — "
                                 "စစ်ပြီးရင် credit ဝင်လာမယ်")
        return True
    if not text:
        return False
    is_menu = text in ("🎬 Video ထုတ်မယ်", "💰 Credit ဖြည့်",
                       "💳 လက်ကျန်", "👤 Username ချိတ်")
    if flow and flow.get("step") == "link" and not is_menu:
        fails = _LINK_FAILS.get(chat_id)
        if fails and fails[1] > time.time():
            tg.send_message(chat_id, "🔒 code အမှား များလို့ ခဏ ပိတ်ထား "
                                     "တယ် — ၃၀ မိနစ် စောင့်ပါ")
            return True
        entry = _LINK_CODES.pop(text.strip(), None)
        _TG_FLOW.pop(chat_id, None)
        if not (entry and entry[1] > time.time()):
            count = (fails[0] + 1) if fails else 1
            until = time.time() + 1800 if count >= 5 else 0
            _LINK_FAILS[chat_id] = (count, until)
        if entry and entry[1] > time.time():
            _LINK_FAILS.pop(chat_id, None)
            accounts.link_tg(entry[0], chat_id)
            user = accounts.get_user(entry[0])
            tg.send_message(
                chat_id, f"✅ ချိတ်ပြီးပြီ — 👤 {user['username']} "
                         f"(💳 {user['credits']} credits)")
        else:
            tg.send_message(chat_id, "❌ code မမှန် (သို့) သက်တမ်း ကုန်ပြီ "
                                     "— web account card မှာ code အသစ် ထုတ်ပါ")
        return True
    if flow and flow.get("step") == "topup_amount" and not is_menu:
        try:
            amount = int(text.strip())
            assert 1 <= amount <= 100000
        except Exception:
            tg.send_message(chat_id, "နံပါတ် ပဲ ပို့ပါ (ဥပမာ 100)")
            return True
        flow["amount"] = amount
        flow["step"] = "topup_proof"
        tg.send_message(chat_id, "📸 အခု လွှဲပြီး screenshot ပို့လိုက်ပါ")
        return True
    if text == "🎬 Video ထုတ်မယ်":
        tg.send_message(chat_id, "🎬 video link (YouTube / TikTok) သို့ "
                                 "video file ကို ဒီမှာ ပို့လိုက်ပါ")
        return True
    if text == "💳 လက်ကျန်":
        if acct:
            tg.send_message(chat_id, f"💳 👤 {acct['username']} — "
                                     f"လက်ကျန် {acct['credits']} credits")
        else:
            tg.send_message(chat_id, "အရင် 👤 Username ချိတ်ပါ — web "
                                     "account card မှာ code ထုတ်ရမယ်" + _LINK_HINT)
        return True
    if text == "👤 Username ချိတ်":
        if acct:
            tg.send_message(chat_id, f"✅ 👤 {acct['username']} နဲ့ "
                                     "ချိတ်ပြီးသား ဖြစ်ပါတယ်")
        else:
            _TG_FLOW[chat_id] = {"step": "link", "ts": time.time()}
            tg.send_message(chat_id, "🔑 web app ရဲ့ 👤 account card မှာ "
                                     "'TG ချိတ်ရန် code' ထုတ်ပြီး အဲဒီ "
                                     "code ကို ဒီမှာ ပို့ပါ" + _LINK_HINT)
        return True
    if text == "💰 Credit ဖြည့်":
        if not acct:
            tg.send_message(chat_id, "အရင် 👤 Username ချိတ်ပါ — "
                                     "ပြီးမှ credit ဖြည့်လို့ရမယ်" + _LINK_HINT)
            return True
        _TG_FLOW[chat_id] = {"step": "topup_amount", "ts": time.time()}
        info = _pay_info()
        tg.send_message(chat_id, "💰 ငွေ လွှဲရန်:\n"
                        + (info or "admin ဆီ နံပါတ် မေးပါ")
                        + "\n\nဘယ်နှ credits ဖြည့်မလဲ — နံပါတ် ပဲ "
                          "ပို့ပါ (ဥပမာ 100)")
        qr = ROOT / "payqr.png"
        if qr.exists():
            try:
                tg.send_photo(chat_id, qr, caption="📸 QR scan ပြီး လွှဲပါ")
            except Exception:
                pass
        return True
    return False


def _tg_flow_prepare(chat_id):
    """Bot flow step 1: fetch the video (the preview cache doubles as
    the job's source, so nothing downloads twice), auto-detect the
    caption band, and ask the user to confirm it on a frame photo."""
    from pipeline import telegram_send as tg
    flow = _TG_FLOW.get(chat_id)
    if not flow:
        return
    try:
        if flow.get("file_id"):
            pid, pdir = _preview_dir("tg-file:" + flow["file_id"])
            pdir.mkdir(parents=True, exist_ok=True)
            src = pdir / "source.mp4"
            if not src.exists():
                tg.download_file(flow["file_id"], src)
        else:
            pid, pdir = _preview_dir(flow["url"])
            pdir.mkdir(parents=True, exist_ok=True)
            src = pdir / "source.mp4"
            if not src.exists():
                dl_mod.download(flow["url"], pdir)
        dur = probe_duration(src)
        flow["minutes"] = max(1, math.ceil(dur / 60.0))
        frame = pdir / "tg_frame.jpg"
        sh(["ffmpeg", "-y", "-v", "error", "-ss",
            f"{max(1.0, dur * 0.3):.1f}", "-i", str(src),
            "-frames:v", "1", str(frame)])
        from pipeline import detect_text
        box = detect_text.detect_text_box(src)
        flow.update({"pid": pid, "src": str(src), "box": box,
                     "step": "blur"})
        rows = []
        if box:
            shown = pdir / "tg_frame_box.jpg"
            detect_text.draw_box(frame, box, shown)
            rows.append([("✅ ဒီနေရာ ဝါး", "b:ok"), ("မဝါးဘူး", "b:no")])
            caption = ("🔍 စာတန်း နေရာ တွေ့တယ် — အနီ အကွက် ကို ဝါးပြီး "
                       "မြန်မာ စာတန်း အဲဒီ အထဲ ထည့်ပေးမယ်။ မှန်လား?")
        else:
            shown = frame
            rows.append([("မဝါးဘူး", "b:no")])
            caption = ("စာတန်း နေရာ auto မတွေ့ဘူး — ဝါးချင်တဲ့ အပိုင်း "
                       "ရွေးပါ (မဝါးချင် 'မဝါးဘူး')")
        rows.append([("အပေါ်", "b:top"), ("အလယ်", "b:mid"),
                     ("အောက်", "b:bot")])
        tg.send_photo(chat_id, shown, caption=caption, buttons=rows)
    except Exception as e:
        _TG_FLOW.pop(chat_id, None)
        try:
            tg.send_message(chat_id, "❌ video ယူလို့ မရပါ: " + str(e)[:200])
        except Exception:
            pass


def _on_tg_callback(cq):
    """Inline-button taps for the bot flow: blur spot, then mode."""
    from pipeline import telegram_send as tg
    tg.answer_callback(cq.get("id"))
    chat = (cq.get("message") or {}).get("chat") or {}
    chat_id = chat.get("id") or (cq.get("from") or {}).get("id")
    data = cq.get("data") or ""
    if data.startswith("t:"):
        # Top-up decision — admin chat only, exactly-once in the DB.
        if not tg.is_admin(chat_id):
            return
        res = None
        act = ""
        try:
            _, tid, act = data.split(":")
            res = accounts.decide_topup(int(tid), act == "ok")
        except Exception:
            res = None
        if res:
            if res.get("tg_chat_id"):
                try:
                    tg.send_message(
                        res["tg_chat_id"],
                        f"💳 Credit {res['credits']} ဝင်ပြီ — "
                        "ကျေးဇူးတင်ပါတယ်!" if act == "ok" else
                        "❌ Credit ဖြည့် တောင်းဆိုချက် ငြင်းခံရပါတယ်")
                except Exception:
                    pass
            tg.send_message(chat_id, f"ဆုံးဖြတ်ပြီး (#{tid})")
        return
    flow = _TG_FLOW.get(chat_id)
    if not flow:
        return
    if time.time() - flow.get("ts", 0) > 30 * 60:
        _TG_FLOW.pop(chat_id, None)
        tg.send_message(chat_id, "⏰ အချိန် ကုန်သွားပြီ — link ပြန်ပို့ပါ")
        return
    if data.startswith("b:") and flow.get("step") == "blur":
        choice = data[2:]
        if choice == "ok":
            flow["blur"] = flow.get("box")
        elif choice == "no":
            flow["blur"] = None
        else:
            flow["blur"] = _BLUR_PRESETS.get(choice)
        flow["step"] = "mode"
        tg.send_message(chat_id, "ဘယ် ပုံစံ ထုတ်မလဲ?",
                        buttons=[[("နှစ်မျိုးလုံး", "m:both")],
                                 [("စာတန်း ပဲ", "m:subs"),
                                  ("အသံ ပဲ", "m:dub")]])
        return
    if data.startswith("m:") and flow.get("step") == "mode":
        mode = data[2:]
        if mode not in ("both", "subs", "dub"):
            return
        flow["mode"] = mode
        flow["step"] = "frame"
        tg.send_message(chat_id, "Frame (ဗီဒီယို အချိုး) ရွေးပါ",
                        buttons=[[("မူရင်း", "f:orig"), ("9:16", "f:9:16"),
                                  ("16:9", "f:16:9")],
                                 [("1:1", "f:1:1"), ("4:5", "f:4:5")]])
        return
    if data.startswith("f:") and flow.get("step") == "frame":
        ratio = data[2:]
        if ratio != "orig" and ratio not in fx_mod.ASPECT_DIMS:
            return
        acct = accounts.user_for_tg(chat_id)
        if not tg.is_admin(chat_id):
            if not acct:
                _TG_FLOW.pop(chat_id, None)
                tg.send_message(chat_id, "အရင် 👤 Username ချိတ် "
                                         "(Menu ထဲက) ပါ — account "
                                         "ရှိမှ job ထုတ်လို့ရမယ်" + _LINK_HINT)
                return
            need = flow.get("minutes") or 1
            if (acct["credits"] or 0) < need:
                _TG_FLOW.pop(chat_id, None)
                tg.send_message(chat_id,
                                f"💳 credit မလောက် — ဒီ video က {need} "
                                f"credits လိုတယ်, လက်ကျန် "
                                f"{acct['credits']} ပဲ ရှိတယ်")
                return
        if (not tg.is_admin(chat_id)
                and _tg_daily_count(chat_id) >= _TG_DAILY_CAP):
            _TG_FLOW.pop(chat_id, None)
            tg.send_message(chat_id, "🚫 ဒီနေ့ အတွက် job ကန့်သတ် ပြည့်ပြီ "
                                     "— မနက်ဖြန် ပြန်လာပါ")
            return
        _TG_FLOW.pop(chat_id, None)
        job, err = _new_job({"url": flow.get("url")
                             or "https://telegram.org/file",
                             "mode": flow.get("mode") or "both"})
        if err:
            tg.send_message(chat_id, "❌ " + err)
            return
        if flow.get("file_id"):
            job["url"] = "📁 Telegram file"
        if acct and not tg.is_admin(chat_id):
            job["user_id"] = acct["id"]
        job["source_path"] = flow.get("src")
        fx = {}
        if flow.get("blur"):
            fx["blur"] = {**flow["blur"], "strength": 25}
        if ratio != "orig":
            fx["aspect"] = {"ratio": ratio, "style": "blur"}
        if fx:
            job["fx"] = fx
        job["tg_chat_id"] = chat_id
        job["tg_from"] = flow.get("from")
        JOBS[job["id"]] = job
        _save(job)
        _tg_daily_count(chat_id, add=True)
        _TG_PENDING.append(job["id"])
        if _any_active_worker() or len(_TG_PENDING) > 1:
            tg.send_message(chat_id,
                            f"✅ လက်ခံရပြီ — တန်းစီ {len(_TG_PENDING)} ခုမြောက်, "
                            "အရင် job တွေ ပြီးရင် စပါမယ်")
        else:
            tg.send_message(chat_id,
                            "✅ လက်ခံရပြီ — စလုပ်နေပါပြီ, ပြီးရင် ဒီမှာပဲ ပို့ပေးပါမယ်")


def _tg_progress(job):
    """Tell the chat which step a bot job is on (changes only)."""
    from pipeline import telegram_send as tg
    chat_id = job.get("tg_chat_id")
    if not chat_id:
        return
    last = None
    while job.get("status") in ("queued", "running"):
        cur = None
        for s in job.get("steps") or []:
            if s.get("state") == "running":
                cur = s.get("label")
                break
        if cur and cur != last:
            last = cur
            try:
                tg.send_message(chat_id, f"⏳ {cur}…")
            except Exception:
                pass
        time.sleep(5)


def _tg_dispatcher():
    """Start queued bot jobs one at a time (the server runs a single
    heavy job safely; the web form is gated the same way)."""
    while True:
        try:
            if _TG_PENDING and not _any_active_worker():
                jid = _TG_PENDING.pop(0)
                job = JOBS.get(jid)
                if job and job.get("status") == "queued":
                    job["worker_started"] = True
                    threading.Thread(target=_worker, args=(job,),
                                     daemon=True).start()
                    threading.Thread(target=_tg_progress, args=(job,),
                                     daemon=True).start()
        except Exception:
            pass
        time.sleep(4)


def _cleanup_job(jid):
    CLEANUP_TIMERS.pop(jid, None)
    job = JOBS.pop(jid, None)
    url = (job or {}).get("url")
    if not url:
        try:
            meta = json.loads((JOBS_DIR / jid / "job.json").read_text(encoding="utf-8"))
            url = meta.get("url")
        except Exception:
            url = None
    shutil.rmtree(JOBS_DIR / jid, ignore_errors=True)
    if url:
        _, pdir = _preview_dir(url)
        shutil.rmtree(pdir, ignore_errors=True)


def _charge_for_job(job):
    """Credits: a non-admin account pays 1 credit per STARTED minute
    of the source video. Charged once — after phase 1, when the
    duration is known, before the expensive phase 2. Admin accounts
    and anonymous (pre-accounts) jobs are never charged."""
    uid = job.get("user_id")
    if not uid or job.get("charged_minutes"):
        return
    user = accounts.get_user(uid)
    if not user or user["role"] == "admin":
        return
    src = JOBS_DIR / job["id"] / "source.mp4"
    try:
        minutes = max(1, math.ceil(probe_duration(src) / 60.0))
    except Exception:
        minutes = 1
    if not accounts.charge(uid, minutes, job["id"]):
        raise RuntimeError(
            f"credit မလောက် — {minutes} credits လိုတယ်, "
            f"လက်ကျန် {user['credits']} ပဲ ရှိတယ်")
    job["charged_minutes"] = minutes
    _save(job)


def _refund_charge(job):
    uid = job.get("user_id")
    minutes = job.get("charged_minutes") or 0
    if uid and minutes:
        accounts.refund(uid, minutes, job["id"])
        job["charged_minutes"] = 0


def _fail(job, e):
    _refund_charge(job)
    job["status"] = "error"
    job["error"] = str(e)[-1500:]
    for s in job["steps"]:
        if s["key"] == job.get("current") and s["state"] == "running":
            s["state"] = "error"
    job["finished_at"] = time.time()
    _schedule_cleanup(job)
    _save(job)
    _tg_error_async(job)


def _run_phase2(job):
    workdir = JOBS_DIR / job["id"]
    run_job.run_phase2(workdir, mode=job["mode"], voice=job["voice"],
                       progress=_progress_fn(job), fx=job.get("fx"),
                       font=job.get("font") or "noto",
                       voice_rate=job.get("voice_rate", 0),
                       voice_pitch=job.get("voice_pitch", 0),
                       subs=job.get("subs"),
                       max_speed=job.get("speed_cap") or 1.3)


def _finish(job):
    job["status"] = "done"
    job["current"] = None
    job["finished_at"] = time.time()
    _schedule_cleanup(job)
    _save(job)
    _tg_deliver_async(job)


def _cancelled(job):
    _refund_charge(job)
    job["status"] = "cancelled"
    job["error"] = "ရပ်လိုက်ပြီ — ဖျက်ရန် တောင်းဆိုထားသည်"
    job["finished_at"] = time.time()
    _save(job)
    t = threading.Timer(20, _cleanup_job, args=(job["id"],))
    t.daemon = True
    t.start()


def _worker(job):
    """Full run: download -> transcribe -> translate -> TTS/retime/burn.

    No mandatory review stop: the auto-translated Myanmar text is used
    as-is and the video renders straight through. The user can still
    edit the text afterwards and re-render (see job_review)."""
    workdir = JOBS_DIR / job["id"]
    threading.Thread(target=_heartbeat, args=(job,), daemon=True).start()
    try:
        job["status"] = "running"
        _save(job)
        run_job.run_phase1(workdir, job["url"], source_path=job.get("source_path"),
                           progress=_progress_fn(job))
        _charge_for_job(job)
        _run_phase2(job)
        _finish(job)
    except JobCancelled:
        _cancelled(job)
    except Exception as e:  # surface the real error to the UI
        _fail(job, e)


def _worker2(job):
    """Re-render phase 2 from the current (possibly edited) segments."""
    threading.Thread(target=_heartbeat, args=(job,), daemon=True).start()
    try:
        job["status"] = "running"
        _save(job)
        _run_phase2(job)
        _finish(job)
    except JobCancelled:
        _cancelled(job)
    except Exception as e:
        _fail(job, e)


@app.get("/")
def index():
    return send_from_directory(ROOT / "static", "index.html")


@app.get("/static/<path:filename>")
def static_files(filename):
    # static_folder is disabled above; serve the PWA manifest/icons explicitly.
    return send_from_directory(ROOT / "static", filename)


@app.get("/sw.js")
def service_worker():
    # Served from the root so its scope covers the whole app.
    return send_from_directory(ROOT / "static", "sw.js")


@app.get("/api/voices")
def voices():
    try:
        import asyncio
        import edge_tts
        proxy = (os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
                 or os.environ.get("http_proxy") or None)
        vs = asyncio.run(edge_tts.list_voices(proxy=proxy))
        mine = [{"id": v["ShortName"],
                 "label": ("အမျိုးသမီး (Nilar)" if "Nilar" in v["ShortName"] else "အမျိုးသား (Thiha)")}
                for v in vs if v["Locale"] == "my-MM"]
        if mine:
            return jsonify(mine)
    except Exception:
        pass
    return jsonify([{"id": v, "label": v} for v in tts.FALLBACK_VOICES])


def _preview_dir(url):
    pid = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return pid, PREVIEWS_DIR / pid


@app.post("/api/preview")
def make_preview():
    """Download (once, cached by URL) and report size/duration for the editor."""
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "လင့်ခ် မမှန်ပါ"}), 400
    pid, pdir = _preview_dir(url)
    pdir.mkdir(parents=True, exist_ok=True)
    src = pdir / "source.mp4"
    try:
        if not src.exists():
            dl_mod.download(url, pdir)
        dur = probe_duration(src)
        w, h = fx_mod.video_size(src)
    except Exception as e:
        return jsonify({"error": str(e)[-500:]}), 502
    title = None
    info_p = pdir / "source_info.json"
    if info_p.exists():
        try:
            title = json.loads(info_p.read_text(encoding="utf-8")).get("title")
        except Exception:
            pass
    return jsonify({"preview_id": pid, "duration": round(dur, 2),
                    "width": w, "height": h, "title": title})


@app.get("/api/preview/<pid>/frame")
def preview_frame(pid):
    src = PREVIEWS_DIR / pid / "source.mp4"
    if not src.exists():
        return jsonify({"error": "preview မတွေ့ပါ"}), 404
    try:
        t = float(request.args.get("t", "2"))
    except ValueError:
        t = 2.0
    dur = probe_duration(src)
    t = min(max(t, 0.0), max(dur - 0.15, 0.0))
    frame = PREVIEWS_DIR / pid / f"frame_{t:.1f}.jpg"
    if not frame.exists():
        sh(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(src),
            "-frames:v", "1", "-q:v", "3", str(frame)])
    return send_file(frame, mimetype="image/jpeg")


@app.post("/api/logo")
def upload_logo():
    f = request.files.get("logo")
    if not f or not f.filename:
        return jsonify({"error": "logo ဖိုင် မပါပါ"}), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        return jsonify({"error": "PNG/JPG/WebP ပဲ ရပါတယ်"}), 400
    lid = uuid.uuid4().hex[:12]
    f.save(UPLOADS_DIR / f"{lid}{ext}")
    return jsonify({"logo_id": lid})


def _build_fx(data):
    """Normalize the editor payload into the fx dict run_job/pipeline.fx eats."""
    fx_in = data.get("fx") or {}
    fx = {}
    blur = fx_in.get("blur")
    if blur:
        fx["blur"] = {"x": float(blur["x"]), "y": float(blur["y"]),
                      "w": float(blur["w"]), "h": float(blur["h"]),
                      "strength": float(blur.get("strength") or 20)}
    logo_in = fx_in.get("logo") or {}
    logo_id = logo_in.get("logo_id")
    if logo_id:
        matches = list(UPLOADS_DIR.glob(f"{logo_id}.*"))
        if matches:
            fx["logo"] = {"path": str(matches[0]),
                          "pos_x": float(logo_in.get("pos_x", 1.0)),
                          "pos_y": float(logo_in.get("pos_y", 0.0)),
                          "size_pct": float(logo_in.get("size_pct", 18)),
                          "opacity": float(logo_in.get("opacity", 70)),
                          "saturation": float(logo_in.get("saturation", 100))}
    asp_in = fx_in.get("aspect") or {}
    if asp_in.get("ratio") in fx_mod.ASPECT_DIMS:
        style = asp_in.get("style")
        fx["aspect"] = {"ratio": asp_in["ratio"],
                        "style": style if style in fx_mod.ASPECT_STYLES
                        else "blur"}
    return fx


FONT_LABELS = {
    "noto": "Noto Sans Myanmar — ထူ (ရုပ်ရှင်ပုံစံ မူလ)",
    "pyidaungsu": "ပြည်ထောင်စု (Pyidaungsu)",
    "padauk": "ပိတောက် (Padauk Bold)",
}


@app.get("/api/fonts")
def fonts():
    from pipeline import burn as burn_mod
    return jsonify([{"id": k, "label": FONT_LABELS.get(k, k)}
                    for k in burn_mod.FONT_CHOICES])


# ---------- Gemini key (server-side only) ----------
# The user pastes the key into THIS page (behind the app's login) — it
# must never travel through chat. It is stored in the project .env
# (chmod 600, survives app updates, never in git) and no endpoint ever
# returns the value, only whether one is configured.

@app.get("/api/settings/gemini-key")
def gemini_key_status():
    from pipeline import gemini_translate
    return jsonify({"configured": gemini_translate.key_configured()})


@app.post("/api/settings/gemini-key")
def gemini_key_save():
    data = request.get_json(force=True) or {}
    key = (data.get("key") or "").strip()
    # Both Gemini key formats are valid: legacy "AIza…" keys and the
    # "AQ.…" auth keys AI Studio has issued since May 2026 (~53 chars).
    ok_format = (key.startswith("AIza") or key.startswith("AQ.")) \
        and 20 <= len(key) <= 80 \
        and all(ch.isalnum() or ch in "._-" for ch in key)
    if not ok_format:
        return jsonify({"error": "key မမှန်ပါ — Gemini key (AQ. သို့ AIza နဲ့ စတာ) ဖြစ်ရပါမယ်"}), 400
    env_path = ROOT / ".env"
    lines = []
    if env_path.exists():
        lines = [ln for ln in env_path.read_text(encoding="utf-8").splitlines()
                 if not ln.startswith("GEMINI_API_KEY=")]
    lines.append(f"GEMINI_API_KEY={key}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(env_path, 0o600)
    except Exception:
        pass
    return jsonify({"ok": True, "configured": True})


@app.delete("/api/settings/gemini-key")
def gemini_key_delete():
    env_path = ROOT / ".env"
    if env_path.exists():
        lines = [ln for ln in env_path.read_text(encoding="utf-8").splitlines()
                 if not ln.startswith("GEMINI_API_KEY=")]
        env_path.write_text("\n".join(lines) + ("\n" if lines else ""),
                            encoding="utf-8")
    return jsonify({"ok": True, "configured": False})


# ---------- Telegram delivery (commercial direction) ----------
# Finished videos can be sent straight to Telegram users from the
# app. Telegram's rule: a bot may only message people who messaged
# it first, so recipients are people who pressed Start on the bot
# (pipeline.telegram_send polls for them). The bot token is set
# here, stored in .env like the Gemini key, never returned.

def _env_write(key, value):
    env_path = ROOT / ".env"
    lines = []
    if env_path.exists():
        lines = [ln for ln in env_path.read_text(encoding="utf-8").splitlines()
                 if not ln.startswith(key + "=")]
    lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(env_path, 0o600)
    except Exception:
        pass


def _env_remove(key):
    env_path = ROOT / ".env"
    if env_path.exists():
        lines = [ln for ln in env_path.read_text(encoding="utf-8").splitlines()
                 if not ln.startswith(key + "=")]
        env_path.write_text("\n".join(lines) + ("\n" if lines else ""),
                            encoding="utf-8")


@app.get("/api/settings/telegram")
def telegram_status():
    from pipeline import telegram_send as tg
    info = {"configured": bool(tg.token()), "bot": None,
            "recipients": len(tg.recipients()),
            "poller": tg.poller_status(), "payinfo": _pay_info(),
            "bot_open": _bot_open(), "wall_flip": _wall_flip()}
    if info["configured"]:
        try:
            info["bot"] = (tg.get_me() or {}).get("username")
        except Exception:
            info["bot"] = None
    return jsonify(info)


@app.post("/api/settings/telegram")
def telegram_save():
    from pipeline import telegram_send as tg
    data = request.get_json(force=True) or {}
    tok = (data.get("token") or "").strip()
    if not tg.valid_format(tok):
        return jsonify({"error": "token မမှန်ပါ — BotFather က ရတဲ့ token ပုံစံ မဟုတ်ပါ"}), 400
    try:
        me = tg.get_me(tok)  # a bad token must not be saved
    except Exception as e:
        return jsonify({"error": f"token စစ်လို့ မရပါ: {e}"}), 400
    _env_write("TELEGRAM_BOT_TOKEN", tok)
    tg.start_poller()
    return jsonify({"ok": True, "bot": (me or {}).get("username")})


@app.delete("/api/settings/telegram")
def telegram_delete():
    _env_remove("TELEGRAM_BOT_TOKEN")
    return jsonify({"ok": True, "configured": False})


@app.get("/api/telegram/recipients")
def telegram_recipients():
    from pipeline import telegram_send as tg
    return jsonify({"recipients": tg.recipients()})


# ------------------------------------------------------------------
# Web accounts (commercial stage). SHADOW MODE by default: accounts
# can be created and used, but nothing is gated on them yet — the
# login wall still guards the site while the owner registers and
# claims the admin account. Setting VT_ACCOUNTS=1 (at the wall-flip
# deploy) turns enforcement on: /api then needs a session, users see
# only their own jobs, settings need the admin role. Loopback calls
# without a forwarded-for header (the serve.py job gate) stay exempt.
ACCOUNTS_ENFORCED = os.environ.get("VT_ACCOUNTS") == "1"
_SESSION_COOKIE = "vt_session"


def _current_user():
    return accounts.user_for_token(request.cookies.get(_SESSION_COOKIE))


def _is_loopback_internal():
    return (request.remote_addr in ("127.0.0.1", "::1")
            and not request.headers.get("X-Forwarded-For"))


@app.before_request
def _accounts_guard():
    if not ACCOUNTS_ENFORCED or _is_loopback_internal():
        return None
    path = request.path
    if not path.startswith("/api/") or path.startswith("/api/auth/"):
        return None
    user = _current_user()
    if path.startswith("/api/settings/") or path.startswith("/api/admin/"):
        if not user or user["role"] != "admin":
            return jsonify({"error": "admin ပဲ ရပါတယ်"}), 403
        return None
    if user is None:
        return jsonify({"error": "အရင် login ဝင်ပါ"}), 401
    return None


def _owns(user, job):
    if not ACCOUNTS_ENFORCED or user is None:
        return True
    return user["role"] == "admin" or job.get("user_id") == user["id"]


def _set_session(resp, token):
    resp.set_cookie(_SESSION_COOKIE, token, max_age=30 * 86400,
                    httponly=True, samesite="Lax",
                    secure=request.is_secure)
    return resp


_REG_ATTEMPTS = {}


def _reg_throttled(ip):
    """At most 10 registrations per hour per source — account
    farming is the cheapest abuse there is."""
    now = time.time()
    hits = [t for t in _REG_ATTEMPTS.get(ip, []) if now - t < 3600]
    hits.append(now)
    _REG_ATTEMPTS[ip] = hits
    return len(hits) > 10


@app.post("/api/auth/register")
def auth_register():
    data = request.get_json(force=True, silent=True) or {}
    user, err = accounts.create_user(data.get("username"),
                                     data.get("password"))
    if err:
        return jsonify({"error": err}), 400
    return _set_session(jsonify({"user": user}),
                        accounts.new_session(user["id"]))


@app.post("/api/auth/login")
def auth_login():
    data = request.get_json(force=True, silent=True) or {}
    ip = (request.headers.get("X-Forwarded-For") or
          request.remote_addr or "")
    user, err = accounts.authenticate(data.get("username"),
                                      data.get("password"), ip)
    if err:
        return jsonify({"error": err}), 401
    return _set_session(jsonify({"user": user}),
                        accounts.new_session(user["id"]))


@app.post("/api/auth/logout")
def auth_logout():
    accounts.revoke(request.cookies.get(_SESSION_COOKIE))
    resp = jsonify({"ok": True})
    resp.delete_cookie(_SESSION_COOKIE)
    return resp


@app.get("/api/auth/me")
def auth_me():
    import sqlite3
    claim_open = False
    try:
        with accounts._db() as conn:
            claim_open = not conn.execute(
                "SELECT 1 FROM users WHERE role = 'admin'").fetchone()
    except sqlite3.Error:
        claim_open = True
    return jsonify({"user": _current_user(),
                    "admin_claim_open": claim_open,
                    "enforced": ACCOUNTS_ENFORCED})


@app.post("/api/auth/change-password")
def auth_change_password():
    user = _current_user()
    if not user:
        return jsonify({"error": "အရင် login ဝင်ပါ"}), 401
    data = request.get_json(force=True, silent=True) or {}
    ok = accounts.set_password(user["id"], data.get("password"))
    if not ok:
        return jsonify({"error": "password အနည်းဆုံး ၆ လုံး ထားပါ"}), 400
    return jsonify({"ok": True})


@app.post("/api/admin/claim")
def admin_claim():
    """One-shot bootstrap: the signed-in account becomes admin, but
    only while no admin exists yet (behind the owner's wall)."""
    user = _current_user()
    if not user:
        return jsonify({"error": "အရင် login ဝင်ပါ"}), 401
    if not accounts.claim_admin(user["username"]):
        return jsonify({"error": "admin ရှိပြီးသား ဖြစ်နေပါတယ်"}), 409
    return jsonify({"user": _current_user()})


def _require_admin():
    user = _current_user()
    if not user or user["role"] != "admin":
        return None
    return user


@app.post("/api/auth/link-code")
def auth_link_code():
    """One-time 10-minute code the user types into the bot to link
    this web account to their Telegram chat (no password ever
    travels through Telegram)."""
    user = _current_user()
    if not user:
        return jsonify({"error": "အရင် login ဝင်ပါ"}), 401
    code = str(secrets.randbelow(900000) + 100000)
    _LINK_CODES[code] = (user["id"], time.time() + 600)
    return jsonify({"code": code})


@app.get("/api/admin/topups")
def admin_topups():
    if not _require_admin():
        return jsonify({"error": "admin ပဲ ရပါတယ်"}), 403
    return jsonify({"topups": accounts.pending_topups()})


@app.post("/api/admin/topups/<int:tid>")
def admin_topup_decide(tid):
    if not _require_admin():
        return jsonify({"error": "admin ပဲ ရပါတယ်"}), 403
    data = request.get_json(force=True, silent=True) or {}
    res = accounts.decide_topup(tid, data.get("decision") == "approved")
    if not res:
        return jsonify({"error": "ဆုံးဖြတ်ပြီးသား (သို့) မတွေ့ပါ"}), 409
    if res.get("tg_chat_id"):
        try:
            from pipeline import telegram_send as tg
            tg.send_message(
                res["tg_chat_id"],
                f"💳 Credit {res['credits']} ဝင်ပြီ — ကျေးဇူးတင်ပါတယ်!"
                if data.get("decision") == "approved" else
                "❌ Credit ဖြည့် တောင်းဆိုချက် ငြင်းခံရပါတယ်")
        except Exception:
            pass
    return jsonify({"ok": True, "result": res})


@app.get("/api/admin/topups/<int:tid>/proof")
def admin_topup_proof(tid):
    if not _require_admin():
        return jsonify({"error": "admin ပဲ ရပါတယ်"}), 403
    top = accounts.get_topup(tid)
    if not top or not top.get("proof"):
        return jsonify({"error": "ပုံ မရှိပါ"}), 404
    return send_file(ROOT / "topups" / top["proof"])


@app.post("/api/settings/payinfo")
def payinfo_save():
    data = request.get_json(force=True, silent=True) or {}
    _env_write("PAY_INFO", (data.get("text") or "").strip()[:300])
    return jsonify({"ok": True})


def _wall_flip():
    p = ROOT / ".env"
    if p.exists():
        for ln in p.read_text(encoding="utf-8").splitlines():
            if ln.startswith("VT_FLIP="):
                return ln.split("=", 1)[1].strip() == "1"
    return False


@app.post("/api/settings/wallflip")
def wallflip_save():
    """Arm/disarm the wall flip. The flag is read by the deploy
    bootstrap: on the next deploy run the Caddy basic-auth wall
    comes down (and accounts enforcement switches on), or the wall
    is restored. Nothing changes until that run — by design, so the
    operator chooses the moment."""
    data = request.get_json(force=True, silent=True) or {}
    _env_write("VT_FLIP", "1" if data.get("flip") else "0")
    return jsonify({"ok": True, "wall_flip": _wall_flip()})


@app.post("/api/settings/botopen")
def botopen_save():
    data = request.get_json(force=True, silent=True) or {}
    _env_write("BOT_OPEN", "1" if data.get("open") else "0")
    return jsonify({"ok": True, "bot_open": _bot_open()})


@app.post("/api/settings/payqr")
def payqr_save():
    """Admin uploads the payment QR image customers scan to top
    up. Stored as payqr.png in the app dir (survives deploys)."""
    f = request.files.get("qr")
    if not f or not f.filename:
        return jsonify({"error": "QR ပုံ မပါပါ"}), 400
    tmp = ROOT / "payqr.tmp"
    f.save(tmp)
    try:
        from PIL import Image
        with Image.open(tmp) as im:
            im.verify()
        with Image.open(tmp) as im:
            im.convert("RGB").save(ROOT / "payqr.png")
    except Exception:
        tmp.unlink(missing_ok=True)
        return jsonify({"error": "ပုံ မဟုတ်ပါ"}), 400
    tmp.unlink(missing_ok=True)
    return jsonify({"ok": True, "qr": True})


@app.delete("/api/settings/payqr")
def payqr_delete():
    (ROOT / "payqr.png").unlink(missing_ok=True)
    return jsonify({"ok": True, "qr": False})


@app.get("/pay-qr.png")
def payqr_file():
    # A payment QR is meant to be shown — public by design.
    p = ROOT / "payqr.png"
    if not p.exists():
        return jsonify({"error": "QR မရှိပါ"}), 404
    return send_file(p, mimetype="image/png")


@app.get("/api/pay-info")
def pay_info_public():
    """What customers see when topping up: the address text + QR."""
    return jsonify({"text": _pay_info(),
                    "qr": (ROOT / "payqr.png").exists()})


@app.get("/api/admin/users")
def admin_users():
    if not _require_admin():
        return jsonify({"error": "admin ပဲ ရပါတယ်"}), 403
    return jsonify({"users": accounts.search_users(
        request.args.get("q", ""))})


@app.delete("/api/admin/users/<int:uid>")
def admin_user_delete(uid):
    if not _require_admin():
        return jsonify({"error": "admin ပဲ ရပါတယ်"}), 403
    me = _current_user()
    if me and me["id"] == uid:
        return jsonify({"error": "ကိုယ့် account ကိုယ် ဖျက်မရပါ"}), 400
    if not accounts.delete_user(uid):
        return jsonify({"error": "ဖျက်မရပါ (admin account ဖြစ် နေ)"}), 400
    return jsonify({"ok": True})


@app.get("/api/admin/users/<int:uid>")
def admin_user_detail(uid):
    if not _require_admin():
        return jsonify({"error": "admin ပဲ ရပါတယ်"}), 403
    user = accounts.get_user(uid)
    if not user:
        return jsonify({"error": "user မတွေ့ပါ"}), 404
    jobs = [j for j in _all_jobs() if j.get("user_id") == uid]
    slim = [{"id": j["id"], "status": j.get("status"),
             "url": j.get("url"), "mode": j.get("mode"),
             "created_at": j.get("created_at"),
             "charged_minutes": j.get("charged_minutes")}
            for j in jobs[:10]]
    return jsonify({"user": user, "job_count": len(jobs),
                    "jobs": slim,
                    "ledger": accounts.ledger_for(uid, 10)})


@app.post("/api/admin/users/<int:uid>")
def admin_user_update(uid):
    if not _require_admin():
        return jsonify({"error": "admin ပဲ ရပါတယ်"}), 403
    data = request.get_json(force=True, silent=True) or {}
    user = accounts.get_user(uid)
    if not user:
        return jsonify({"error": "user မတွေ့ပါ"}), 404
    if "disabled" in data:
        accounts.set_disabled(uid, bool(data["disabled"]))
    if data.get("credits_delta"):
        accounts.adjust_credits(uid, int(data["credits_delta"]))
    if data.get("new_password"):
        if not accounts.set_password(uid, data["new_password"]):
            return jsonify({"error": "password အနည်းဆုံး ၆ လုံး ထားပါ"}), 400
    if data.get("tg_chat_id"):
        accounts.link_tg(uid, int(data["tg_chat_id"]))
    return jsonify({"user": accounts.get_user(uid)})


def _tg_worker(job, chat_id, out_path):
    from pipeline import telegram_send as tg
    try:
        caption = "🎬 မြန်မာ ဘာသာပြန် ဗီဒီယို\n" + (job.get("url") or "")
        tg.send_video(chat_id, out_path, caption=caption)
        job["tg"] = {**(job.get("tg") or {}), "state": "sent"}
    except Exception as e:
        job["tg"] = {**(job.get("tg") or {}), "state": "failed",
                     "error": str(e)[:200]}
    job["updated_at"] = time.time()
    _save(job)


@app.post("/api/jobs/<jid>/send-telegram")
def send_telegram(jid):
    from pipeline import telegram_send as tg
    job = _load_job(jid)
    if not job:
        return jsonify({"error": "job မတွေ့ပါ"}), 404
    out = JOBS_DIR / jid / "output.mp4"
    if not out.exists():
        return jsonify({"error": "ဗီဒီယို မပြီးသေးပါ"}), 400
    data = request.get_json(force=True) or {}
    chat_id = data.get("chat_id")
    if not chat_id or not tg.is_recipient(chat_id):
        return jsonify({"error": "လက်ခံသူ မတွေ့ပါ — bot ကို Start နှိပ်ထားသူ ဖြစ်ရပါမယ်"}), 400
    size = out.stat().st_size
    if size > tg.MAX_BYTES:
        return jsonify({"error": f"ဖိုင် {size / 1e6:.0f}MB ရှိနေတယ် — Telegram bot က 50MB အထိပဲ ပို့လို့ရပါတယ်"}), 400
    user = next((r for r in tg.recipients()
                 if r["chat_id"] == int(chat_id)), {})
    job["tg"] = {"state": "sending",
                 "to": user.get("username") or user.get("name") or str(chat_id)}
    _save(job)
    threading.Thread(target=_tg_worker, args=(job, int(chat_id), out),
                     daemon=True).start()
    return jsonify({"ok": True, "state": "sending"})


def _clamp_num(v, lo, hi, default):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _build_subs(data):
    """Subtitle layout params, clamped server-side; absent = previous look
    (burn.SUBS_DEFAULTS: size 100, width 95, bottom 10, bottom-aligned)."""
    s = data.get("subs") or {}
    return {
        "size_pct": _clamp_num(s.get("size_pct"), 60, 160, 100),
        "width_pct": _clamp_num(s.get("width_pct"), 50, 100, 95),
        "bottom_pct": _clamp_num(s.get("bottom_pct"), 2, 25, 10),
        "align": "top" if s.get("align") == "top" else "bottom",
    }


def _new_job(data):
    """Build a job record from request-style data, or (None, error).
    Shared by the web form and the Telegram bot, so a job is a job
    however it arrives. The caller registers it in JOBS and decides
    when its worker starts."""
    url = (data.get("url") or "").strip()
    mode = data.get("mode") or "both"
    voice = data.get("voice") or tts.DEFAULT_VOICE
    font = data.get("font") or "noto"
    subs = _build_subs(data)
    # Voice tuning: percent speed (-50..+100) and Hz pitch (-50..+50);
    # clamped again in pipeline.tts; defaults 0 keep prior behaviour.
    try:
        voice_rate = max(-50, min(100, int(data.get("voice_rate") or 0)))
    except (TypeError, ValueError):
        voice_rate = 0
    try:
        voice_pitch = max(-50, min(50, int(data.get("voice_pitch") or 0)))
    except (TypeError, ValueError):
        voice_pitch = 0
    # Dub speed cap: how far the retime step may speed speech up to
    # fit its window before it must freeze the frame instead
    # (owner's control, 1.0 = never speed up, up to 4.0).
    try:
        speed_cap = max(1.0, min(4.0, float(data.get("speed_cap") or 1.3)))
    except (TypeError, ValueError):
        speed_cap = 1.3
    if font not in FONT_LABELS:
        return None, "font မမှန်ပါ"
    if not url.startswith(("http://", "https://")):
        return None, "လင့်ခ် မမှန်ပါ"
    if mode not in ("subs", "dub", "both"):
        return None, "mode မမှန်ပါ"
    job = {
        "id": uuid.uuid4().hex[:12], "url": url, "mode": mode,
        "voice": voice, "font": font, "subs": subs,
        "voice_rate": voice_rate, "voice_pitch": voice_pitch,
        "speed_cap": speed_cap,
        "fx": _build_fx(data), "source_path": None,
        "status": "queued", "current": None, "error": None,
        "created_at": time.time(), "updated_at": time.time(),
        "finished_at": None,
        "steps": [{"key": k, "label": STEP_LABELS[k], "state": "pending"}
                  for k in run_job.steps_for_mode(mode)],
    }
    return job, None


@app.post("/api/jobs")
def create_job():
    data = request.get_json(force=True)
    job, err = _new_job(data)
    if err:
        return jsonify({"error": err}), 400
    me = _current_user()
    if me:
        job["user_id"] = me["id"]
    preview_id = data.get("preview_id")
    if preview_id:
        # A preview may be reused ONLY for the very link it was taken
        # from (its id is a hash of that link). A stale id left over
        # from a previous video must never substitute the old video
        # for the new link's job.
        expected_pid, _ = _preview_dir(job["url"])
        if str(preview_id) != expected_pid:
            preview_id = None
    if preview_id:
        cand = PREVIEWS_DIR / str(preview_id) / "source.mp4"
        if cand.exists():
            job["source_path"] = str(cand)
    JOBS[job["id"]] = job
    _save(job)
    job["worker_started"] = True
    threading.Thread(target=_worker, args=(job,), daemon=True).start()
    return jsonify(job)


def _job_view(job):
    return {k: job.get(k) for k in
            ("id", "url", "mode", "status", "current", "error", "tg",
             "created_at", "updated_at", "finished_at", "cleanup_at")}


def _all_jobs():
    """In-memory jobs merged with any persisted on disk. A job that
    exists only on disk cannot be running (the process that ran it is
    gone), so a disk-only 'active' status is healed to error rather
    than lying to the UI — and blocking the gate — forever."""
    out = {}
    for p in JOBS_DIR.glob("*/job.json"):
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if j.get("id"):
            out[j["id"]] = j
    for jid, j in JOBS.items():
        out[jid] = j
    res = []
    for jid, j in out.items():
        if jid not in JOBS and j.get("status") in ("queued", "running"):
            j["status"] = "error"
            j["error"] = "server ပြန်စ၍ ရပ်သွား"
            j["finished_at"] = j.get("finished_at") or time.time()
            JOBS[jid] = j
            _save(j)
            _schedule_cleanup(j)
        res.append(j)
    res.sort(key=lambda j: j.get("created_at") or 0, reverse=True)
    return res


@app.get("/api/jobs")
def list_jobs():
    user = _current_user()
    mine = [j for j in _all_jobs() if _owns(user, j)]
    return jsonify({"jobs": [_job_view(j) for j in mine[:10]]})


@app.get("/api/jobs/latest")
def latest_job():
    user = _current_user()
    for j in _all_jobs():
        if not _owns(user, j):
            continue
        if j.get("status") in ("queued", "running", "waiting_review"):
            return jsonify({"job": _job_view(j)})
        age = time.time() - (j.get("finished_at") or j.get("created_at") or 0)
        if age < 20 * 60:
            return jsonify({"job": _job_view(j)})
        break
    return jsonify({"job": None})


@app.get("/api/jobs/<jid>")
def get_job(jid):
    job = _load_job(jid)
    if not job or not _owns(_current_user(), job):
        return jsonify({"error": "job မတွေ့ပါ"}), 404
    return jsonify(job)


def _load_job(jid):
    job = JOBS.get(jid)
    if not job:
        p = JOBS_DIR / jid / "job.json"
        if not p.exists():
            return None
        job = json.loads(p.read_text(encoding="utf-8"))
        JOBS[jid] = job
    return job


@app.get("/api/jobs/<jid>/segments")
def job_segments(jid):
    """Segments for the review/edit step (after phase 1)."""
    job = _load_job(jid)
    if not job:
        return jsonify({"error": "job မတွေ့ပါ"}), 404
    seg_path = JOBS_DIR / jid / "segments_my.json"
    if not seg_path.exists():
        return jsonify({"error": "အပိုင်းတွေ မရသေးပါ"}), 409
    from pipeline.common import load_segments
    segs = load_segments(seg_path)
    return jsonify({"status": job["status"], "segments": [
        {"idx": i, "start": s.get("start"), "end": s.get("end"),
         "text": s.get("text"), "text_my": s.get("text_my"),
         "provider": s.get("provider"), "edited": bool(s.get("edited"))}
        for i, s in enumerate(segs)]})


@app.post("/api/jobs/<jid>/review")
def job_review(jid):
    """Submit edits (or none) and (re-)run phase 2.

    Body: {"edits": [{"idx": 0, "text_my": "..."}, ...]} — segments not
    listed keep their translated text. Allowed on a finished job (the
    normal flow renders straight through; this is the optional
    edit-and-rerun path) and on a legacy waiting_review job.
    """
    job = _load_job(jid)
    if not job:
        return jsonify({"error": "job မတွေ့ပါ"}), 404
    if job["status"] not in ("waiting_review", "done"):
        return jsonify({"error": "ဒီ job က ပြင်ဆင်ရန် အဆင့် မဟုတ်ပါ"}), 409
    data = request.get_json(force=True) or {}
    edits = {int(e["idx"]): str(e["text_my"]) for e in data.get("edits", [])
             if "idx" in e and "text_my" in e}
    from pipeline.common import load_segments, save_segments, write_srt
    seg_path = JOBS_DIR / jid / "segments_my.json"
    segs = load_segments(seg_path)
    changed = 0
    for idx, new_text in edits.items():
        if 0 <= idx < len(segs) and new_text.strip() \
                and new_text.strip() != (segs[idx].get("text_my") or "").strip():
            segs[idx]["text_my"] = new_text.strip()
            segs[idx]["edited"] = True
            changed += 1
    save_segments(segs, seg_path)
    write_srt(segs, JOBS_DIR / jid / "subs_my.srt")  # keep subs-mode SRT in sync
    job["edited_count"] = changed
    # A re-render supersedes the pending auto-cleanup of the old result.
    old_timer = CLEANUP_TIMERS.pop(jid, None)
    if old_timer:
        old_timer.cancel()
    job.pop("cleanup_at", None)
    _save(job)
    job["worker_started"] = True
    threading.Thread(target=_worker2, args=(job,), daemon=True).start()
    return jsonify({"ok": True, "edited_count": changed})


@app.get("/api/jobs/<jid>/download")
def download_job(jid):
    out = JOBS_DIR / jid / "output.mp4"
    if not out.exists():
        return jsonify({"error": "မပြီးသေးပါ"}), 404
    return send_file(out, as_attachment=True, download_name=f"myanmar-{jid}.mp4")


@app.get("/api/jobs/<jid>/stream")
def stream_job(jid):
    """Inline playback for the result preview (the download route's
    attachment header prevents some phone browsers from playing it)."""
    out = JOBS_DIR / jid / "output.mp4"
    if not out.exists():
        return jsonify({"error": "မပြီးသေးပါ"}), 404
    return send_file(out, as_attachment=False)


@app.delete("/api/jobs/<jid>")
def delete_job(jid):
    """Delete a job and all its files right now (the user asked for no
    leftovers; otherwise the 15-minute auto-cleanup does it)."""
    job = _load_job(jid)
    if not job:
        return jsonify({"error": "job မတွေ့ပါ"}), 404
    if job["status"] in ("queued", "running"):
        # Stop it instead of refusing: the worker checks cancel_requested
        # at every step boundary and unwinds; a failsafe timer removes
        # the files even if a step never returns.
        job["cancel_requested"] = True
        _save(job)
        t = threading.Timer(300, _cleanup_job, args=(jid,))
        t.daemon = True
        t.start()
        return jsonify({"ok": True, "cancelling": True})
    old_timer = CLEANUP_TIMERS.pop(jid, None)
    if old_timer:
        old_timer.cancel()
    _cleanup_job(jid)
    return jsonify({"ok": True})


# ---------- text-only export + bulk text input ----------
# Both read/write the SAME store the review API uses (segments_my.json),
# so exports always reflect the user's saved edits, and bulk-pasted text
# is exactly what phase 2 dubs (TTS) and burns (subtitles) in mode "both".

def _segments_or_error(jid):
    """(job, segments, error_response) — segments None when error is set."""
    job = _load_job(jid)
    if not job:
        return None, None, (jsonify({"error": "job မတွေ့ပါ"}), 404)
    seg_path = JOBS_DIR / jid / "segments_my.json"
    if not seg_path.exists():
        return job, None, (jsonify({"error": "စာသားတွေ မရသေးပါ"}), 404)
    from pipeline.common import load_segments
    return job, load_segments(seg_path), None


def _export_lang():
    """(text_key, lang) or None for a bad ?lang= value."""
    lang = request.args.get("lang", "my")
    if lang not in ("my", "src"):
        return None
    return ("text_my" if lang == "my" else "text"), lang


@app.get("/api/jobs/<jid>/export/txt")
def export_txt(jid):
    """Plain text of a job's segments — one segment per paragraph.
    Default: edited Myanmar (text_my); ?lang=src for the source text.
    Works as soon as segments exist; no video render needed."""
    job, segs, err = _segments_or_error(jid)
    if err:
        return err
    got = _export_lang()
    if not got:
        return jsonify({"error": "lang မမှန်ပါ"}), 400
    key, lang = got
    paras = [(s.get(key) or "").strip() for s in segs]
    body = "\n\n".join(p for p in paras if p) + "\n"
    return Response(body, mimetype="text/plain; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{jid}_{lang}.txt"'})


@app.get("/api/jobs/<jid>/export/srt")
def export_srt(jid):
    """Proper SRT from the segments with their timings (sequential indices,
    segments with empty text are skipped and numbering stays sequential)."""
    job, segs, err = _segments_or_error(jid)
    if err:
        return err
    got = _export_lang()
    if not got:
        return jsonify({"error": "lang မမှန်ပါ"}), 400
    key, lang = got
    from pipeline.common import fmt_ts
    lines, n = [], 0
    for s in segs:
        text = (s.get(key) or "").strip()
        if not text:
            continue
        n += 1
        lines += [str(n), f"{fmt_ts(s['start'])} --> {fmt_ts(s['end'])}",
                  text, ""]
    return Response("\n".join(lines), mimetype="text/plain; charset=utf-8",
                    headers={"Content-Disposition":
                             f'attachment; filename="{jid}_{lang}.srt"'})


def _split_bulk_units(text, n_segments):
    """Split pasted text into units to distribute over segments.

    Rule: (1) split by line breaks (non-empty lines); if that count does
    not equal the segment count, (2) re-split the whole text into
    sentences on the enders ။ and '.' and use those instead. Either list
    is then assigned sequentially (see job_review_bulk for over/underflow).
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) == n_segments:
        return lines
    flat = " ".join(lines)
    sents = [p.strip() for p in re.split(r"(?<=[။.])\s*", flat) if p.strip()]
    return sents or lines


@app.post("/api/jobs/<jid>/review/bulk")
def job_review_bulk(jid):
    """Paste a whole translated text at once; it is distributed over the
    segments and saved as review edits (edited=True) WITHOUT starting
    phase 2 — the user can still tweak per-segment boxes, then continue
    with the normal review submit.

    Distribution: units (lines, or sentences — see _split_bulk_units) are
    assigned to segments in order. Extra units beyond the segment count
    are joined into the LAST segment; if there are fewer units than
    segments, the remaining segments keep their current text.
    """
    job = _load_job(jid)
    if not job:
        return jsonify({"error": "job မတွေ့ပါ"}), 404
    if job["status"] not in ("waiting_review", "done"):
        return jsonify({"error": "ဒီ job က ပြင်ဆင်ရန် အဆင့် မဟုတ်ပါ"}), 409
    data = request.get_json(force=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "စာသား ထည့်ပါ"}), 400
    job, segs, err = _segments_or_error(jid)
    if err:
        return err
    if not segs:
        return jsonify({"error": "အပိုင်းတွေ မရှိပါ"}), 409
    units = _split_bulk_units(text, len(segs))
    assign = {}
    if len(units) >= len(segs):
        for i in range(len(segs) - 1):
            assign[i] = units[i]
        assign[len(segs) - 1] = " ".join(units[len(segs) - 1:])
    else:
        for i, u in enumerate(units):
            assign[i] = u
    from pipeline.common import save_segments, write_srt
    changed = 0
    for i, new_text in assign.items():
        if new_text and new_text != (segs[i].get("text_my") or ""):
            segs[i]["text_my"] = new_text
            segs[i]["edited"] = True
            changed += 1
    seg_path = JOBS_DIR / jid / "segments_my.json"
    save_segments(segs, seg_path)
    write_srt(segs, JOBS_DIR / jid / "subs_my.srt")  # keep SRT in sync
    return jsonify({"ok": True, "changed": changed, "segments": [
        {"idx": i, "text_my": s.get("text_my")} for i, s in enumerate(segs)]})


def _startup_sweep():
    """Remove leftovers from before a restart: job folders and preview
    caches older than an hour are deleted (nothing is kept long-term)."""
    now = time.time()
    for d in list(JOBS_DIR.iterdir()):
        if not d.is_dir():
            continue
        ref, url = None, None
        try:
            meta = json.loads((d / "job.json").read_text(encoding="utf-8"))
            ref = meta.get("finished_at") or meta.get("created_at")
            url = meta.get("url")
        except Exception:
            pass
        if ref and now - float(ref) > 3600:
            shutil.rmtree(d, ignore_errors=True)
            if url:
                _, pdir = _preview_dir(url)
                shutil.rmtree(pdir, ignore_errors=True)
    for d in list(PREVIEWS_DIR.iterdir()):
        try:
            if d.is_dir() and now - d.stat().st_mtime > 3600:
                shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


try:
    _startup_sweep()
except Exception:
    pass

try:
    from pipeline import telegram_send as _tg
    _tg.MESSAGE_HANDLER = _on_tg_message
    _tg.CALLBACK_HANDLER = _on_tg_callback
    _tg.start_poller()
except Exception:
    pass

# Each background loop gets its own guard: one failure must never
# silently take the others down with it.
for _target in (_backup_loop, _tg_dispatcher):
    try:
        threading.Thread(target=_target, daemon=True).start()
    except Exception:
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8611"))
    app.run(host="0.0.0.0", port=port, threaded=True)
