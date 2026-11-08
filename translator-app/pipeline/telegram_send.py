"""Telegram delivery for finished videos (commercial direction).

How Telegram bots work, and why this module is shaped this way:
  * A bot can only message a user who has messaged the bot FIRST
    (there is no API to resolve a @username to a chat id). So the app
    runs a small getUpdates poller: anyone who opens the bot and taps
    Start (or sends any message) is registered as a recipient.
  * The bot token lives in the app .env (TELEGRAM_BOT_TOKEN), set from
    the app's own settings card — it never travels through chat and no
    endpoint ever returns it.
  * api.telegram.org caps bot uploads at 50 MB; the caller enforces
    that before sending.

Recipients persist in .tg_recipients.json in the app dir (survives
app updates like .env does; never in git).
"""
import json
import os
import re
import threading
import time
from pathlib import Path

_API = "https://api.telegram.org/bot{token}/{method}"
_LOCK = threading.Lock()
_ME_CACHE = {}
_POLLER_STARTED = False

WELCOME = ("✅ မှတ်ပုံတင်ပြီးပါပြီ!\n"
           "🎬 ဘာသာပြန်ချင်တဲ့ video link (YouTube / TikTok) ကို "
           "ဒီမှာ ပို့လိုက်ပါ — ပြီးသွားရင် ဗီဒီယို ဒီ chat ထဲ "
           "ပြန်ပို့ပေးပါမယ်။")

# Diagnostics for the settings card: when the poller last completed
# a poll and what it last choked on (if anything).
LAST_POLL = {"at": 0.0, "error": ""}


_POLLER_THREAD = None


def poller_status():
    return {"alive": bool(_POLLER_THREAD and _POLLER_THREAD.is_alive()),
            "last_poll_ago": (round(time.time() - LAST_POLL["at"])
                              if LAST_POLL["at"] else None),
            "last_error": LAST_POLL["error"][:120]}


# Set by the app: called with every incoming message dict (after
# registration), so the app can treat links as new jobs.
MESSAGE_HANDLER = None

# Set by the app: called with every callback_query dict (inline
# button taps), so the app can run choice flows (blur spot, mode).
CALLBACK_HANDLER = None

# The owner's own Telegram chat id, fixed in code at their order
# (2026-11-08: "ငါ့ tg ကို admin ပုံသေ") — the same pattern as the
# MLBB app's fixed admin Gmail. Only this chat may submit jobs for
# now; everyone else may register (future customers) and receive.
# A chat id never changes; if the owner ever changes Telegram
# accounts, this one constant is the edit.
FIXED_ADMIN_CHAT_ID = 6526641203


def is_admin(chat_id):
    return str(chat_id) == str(FIXED_ADMIN_CHAT_ID)


def _root():
    return Path(os.environ.get("VT_APP_ROOT")
                or Path(__file__).resolve().parent.parent)


def _env_path():
    return _root() / ".env"


def token():
    """Bot token from the environment or the app .env, or None."""
    t = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if t:
        return t
    p = _env_path()
    if p.exists():
        for ln in p.read_text(encoding="utf-8").splitlines():
            if ln.startswith("TELEGRAM_BOT_TOKEN="):
                return ln.split("=", 1)[1].strip() or None
    return None


def valid_format(tok):
    return bool(tok) and bool(
        re.fullmatch(r"\d{6,12}:[A-Za-z0-9_-]{30,}", tok.strip()))


def _call(tok, method, timeout=30, **params):
    import requests
    r = requests.post(_API.format(token=tok, method=method),
                      data=params, timeout=timeout)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(
            f"telegram {method}: {data.get('description') or r.status_code}")
    return data["result"]


def get_me(tok=None):
    """Bot identity (cached per token) — also the token validity check."""
    tok = tok or token()
    if not tok:
        return None
    if tok not in _ME_CACHE:
        _ME_CACHE[tok] = _call(tok, "getMe", timeout=15)
    return _ME_CACHE[tok]


# ---------------- recipients ----------------

def _store_path():
    return _root() / ".tg_recipients.json"


def _load_store():
    try:
        return json.loads(_store_path().read_text(encoding="utf-8"))
    except Exception:
        return {"offset": 0, "users": {}}


def _save_store(store):
    p = _store_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def recipients():
    with _LOCK:
        store = _load_store()
    out = [{"chat_id": int(cid), "username": u.get("username"),
            "name": u.get("name")}
           for cid, u in store.get("users", {}).items()]
    out.sort(key=lambda r: (r["username"] or r["name"] or "").lower())
    return out


def is_recipient(chat_id):
    with _LOCK:
        store = _load_store()
    return str(chat_id) in store.get("users", {})


def poll_once():
    """One getUpdates pass: register anyone who talked to the bot and
    greet first-timers. Returns how many updates were consumed."""
    tok = token()
    if not tok:
        return 0
    import requests
    with _LOCK:
        store = _load_store()
    r = requests.get(_API.format(token=tok, method="getUpdates"),
                     params={"offset": store.get("offset", 0),
                             "timeout": 0,
                             "allowed_updates": '["message", "callback_query"]'},
                     timeout=20)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(
            f"telegram getUpdates: {data.get('description')}")
    LAST_POLL["at"] = time.time()
    LAST_POLL["error"] = ""
    updates = data.get("result") or []
    new_users = []
    for u in updates:
        store["offset"] = max(store.get("offset", 0), u["update_id"] + 1)
        msg = u.get("message") or {}
        if not msg and u.get("callback_query"):
            # a button tap also counts as contact: register the tapper
            cq = u["callback_query"]
            frm = cq.get("from") or {}
            chat = (cq.get("message") or {}).get("chat") or {}
            msg = {"from": frm, "chat": chat}
        frm, chat = msg.get("from") or {}, msg.get("chat") or {}
        if frm.get("is_bot") or not chat.get("id"):
            continue
        cid = str(chat["id"])
        if cid not in store["users"]:
            store["users"][cid] = {
                "username": frm.get("username"),
                "name": " ".join(x for x in [frm.get("first_name"),
                                             frm.get("last_name")] if x),
                "first_seen": time.time(),
            }
            new_users.append(cid)
        store["users"][cid]["last_seen"] = time.time()
    if updates:
        with _LOCK:
            _save_store(store)
    for cid in new_users:
        try:
            _call(tok, "sendMessage", chat_id=cid, text=WELCOME)
        except Exception:
            pass  # registration stands even if the greeting fails
    if MESSAGE_HANDLER:
        for u in updates:
            msg = u.get("message")
            if msg:
                try:
                    MESSAGE_HANDLER(msg)
                except Exception:
                    pass
    if CALLBACK_HANDLER:
        for u in updates:
            cq = u.get("callback_query")
            if cq:
                try:
                    CALLBACK_HANDLER(cq)
                except Exception:
                    pass
    return len(updates)


def send_message(chat_id, text, buttons=None):
    tok = token()
    if not tok:
        raise RuntimeError("telegram bot token မရှိပါ")
    params = {"chat_id": str(chat_id), "text": text}
    if buttons:
        params["reply_markup"] = _markup(buttons)
    return _call(tok, "sendMessage", **params)


def _markup(buttons):
    """buttons: rows of (label, callback_data) -> inline keyboard JSON."""
    return json.dumps({"inline_keyboard": [
        [{"text": label, "callback_data": data} for label, data in row]
        for row in buttons]})


def send_photo(chat_id, photo_path, caption="", buttons=None):
    tok = token()
    if not tok:
        raise RuntimeError("telegram bot token မရှိပါ")
    import requests
    data = {"chat_id": str(chat_id), "caption": caption[:1024]}
    if buttons:
        data["reply_markup"] = _markup(buttons)
    with open(photo_path, "rb") as fh:
        r = requests.post(_API.format(token=tok, method="sendPhoto"),
                          data=data,
                          files={"photo": (Path(photo_path).name, fh,
                                           "image/jpeg")},
                          timeout=120)
    payload = r.json()
    if not payload.get("ok"):
        raise RuntimeError(
            f"telegram sendPhoto: {payload.get('description')}")
    return payload["result"]


def answer_callback(callback_query_id):
    tok = token()
    if not tok:
        return
    try:
        _call(tok, "answerCallbackQuery",
              callback_query_id=str(callback_query_id), timeout=10)
    except Exception:
        pass


def start_poller():
    """Background thread keeping the recipient list current. Runs for
    the life of the process; idles quietly while no token is set.
    Safe to call again (e.g. right after a token is saved): a dead
    thread is restarted, a live one is left alone."""
    global _POLLER_THREAD
    if _POLLER_THREAD and _POLLER_THREAD.is_alive():
        return

    def loop():
        while True:
            try:
                if token():
                    poll_once()
            except Exception as e:
                LAST_POLL["error"] = f"{type(e).__name__}: {e}"[:200]
            time.sleep(5)

    _POLLER_THREAD = threading.Thread(target=loop, daemon=True)
    _POLLER_THREAD.start()


def download_file(file_id, dest_path):
    """Bot file download (getFile). Telegram refuses bot downloads
    over 20 MB — the caller checks the size before calling."""
    tok = token()
    if not tok:
        raise RuntimeError("telegram bot token မရှိပါ")
    info = _call(tok, "getFile", file_id=str(file_id))
    path = info.get("file_path")
    if not path:
        raise RuntimeError("telegram file မရပါ")
    import requests
    url = f"https://api.telegram.org/file/bot{tok}/{path}"
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(dest_path, "wb") as fh:
            for chunk in r.iter_content(chunk_size=1 << 18):
                fh.write(chunk)
    return dest_path


# ---------------- sending ----------------

MAX_BYTES = 50 * 1000 * 1000  # api.telegram.org bot upload cap


def send_video(chat_id, path, caption=""):
    """Upload the finished mp4 to a registered recipient. sendVideo
    first (plays inline in Telegram); some files Telegram refuses as
    video get one retry as a plain document."""
    tok = token()
    if not tok:
        raise RuntimeError("telegram bot token မရှိပါ")
    import requests
    path = Path(path)
    last_err = None
    for method, field in (("sendVideo", "video"), ("sendDocument", "document")):
        try:
            with open(path, "rb") as fh:
                r = requests.post(
                    _API.format(token=tok, method=method),
                    data={"chat_id": str(chat_id), "caption": caption[:1024],
                          "supports_streaming": "true"},
                    files={field: (path.name, fh, "video/mp4")},
                    timeout=600)
            data = r.json()
            if data.get("ok"):
                return data["result"]
            last_err = RuntimeError(
                f"telegram {method}: {data.get('description')}")
        except Exception as e:
            last_err = e
    raise RuntimeError(str(last_err))
