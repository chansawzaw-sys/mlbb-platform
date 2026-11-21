"""Video Translator — web server.

Run:  .venv/bin/python app.py        (PORT env, default 8611)
UI:   http://localhost:8611/
API:  POST /api/jobs {url, mode: subs|dub|both, voice_style}
      GET  /api/jobs/<id>            job state + step list
      GET  /api/jobs/<id>/download   finished .mp4
      GET  /api/voices               voice styles (build 200)
"""
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import (Flask, Response, jsonify, request, send_file,
                   send_from_directory)

# CA patch must land before edge_tts/aiohttp are imported anywhere
from pipeline.common import ensure_system_ca

ensure_system_ca()

from pipeline import accounts
from pipeline import download as dl_mod
from pipeline import elevenlabs_tts
from pipeline import envfile
from pipeline import gemini_pool
from pipeline import gemini_tts
from pipeline import fx as fx_mod
from pipeline import commentary as commentary_mod
from pipeline import recap as recap_mod
from pipeline import story as story_mod
from pipeline import story_render
from pipeline import run_job, tts
from pipeline.common import (encode_threads, job_context, probe_duration,
                             run as sh, set_active_jobs_provider,
                             terminate_job_processes)

ROOT = Path(__file__).resolve().parent
JOBS_DIR = ROOT / "jobs"
JOBS_DIR.mkdir(exist_ok=True)
PREVIEWS_DIR = ROOT / "previews"
PREVIEWS_DIR.mkdir(exist_ok=True)
UPLOADS_DIR = ROOT / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)

app = Flask(__name__, static_folder=None)
# Build 193: the single source of truth for the running build. The
# page's footer tag (static/index.html #buildTag) must carry the
# same string — proof193 asserts they are equal — and the page
# polls /api/version so a device still showing an older shell
# reloads itself once instead of sitting on a stale build.
BUILD_TAG = "build 243"
JOBS = {}  # id -> dict (also persisted to jobs/<id>/job.json)

STEP_LABELS = {
    "download": "ဗီဒီယို ဒေါင်းလုပ်",
    "transcribe": "စကားသံကို စာသားထုတ်",
    "translate": "မြန်မာ ဘာသာပြန်",
    "script": "Recap စာသား ရေးရန်",
    "render": "Cartoon ဗီဒီယို ဖန်တီး",
    "tts": "မြန်မာ အသံထုတ်",
    "retime": "အချိန်ကိုက် ညှိ (လိုရင် ဖရိမ်ရပ်)",
    "finalize": "စာတန်းထိုး (ရုပ်ရှင်ပုံစံ) + Logo/Blur + ဗီဒီယို ပေါင်းစပ်",
    # Build 234: the download-only product's second step — the
    # downloaded file is measured, quoted and made ready.
    "save": "ဖိုင် အဆင်သင့် လုပ်ရန်",
}


# ---------- durable job state (build 204, audit V10 + V09) ----------
# _save used to truncate job.json in place, unlocked, while three
# writer classes (worker progress, the 45 s heartbeat, route
# handlers) raced on it: readers routinely parsed a half-written
# file — _all_jobs silently skipped the job (it vanished from the
# UI) and _load_job 500'd. Now each job's saves serialise on a
# per-job lock and land via a unique temp file in the same
# directory + os.replace (the envfile.py build-192 pattern), so a
# reader sees the old snapshot or the new one, never a torn one.
# The tombstone set is the V09 half: once a job's files are
# cleaned up, a late save from a still-unwinding worker must not
# resurrect job.json (and the job) behind the cleanup.
_SAVE_LOCKS = {}
_SAVE_LOCKS_GUARD = threading.Lock()
_DELETED_JOBS = {}  # jid -> True, insertion-ordered; capped below
_DELETED_CAP = 4096


def _save_lock(jid):
    with _SAVE_LOCKS_GUARD:
        lk = _SAVE_LOCKS.get(jid)
        if lk is None:
            lk = _SAVE_LOCKS[jid] = threading.Lock()
        return lk


def _tombstone(jid):
    """Mark a job deleted: future _save calls for it no-op.
    Called at the TOP of _cleanup_job, before any file is removed,
    so the check-then-write window in _save stays closed."""
    _DELETED_JOBS[jid] = True
    while len(_DELETED_JOBS) > _DELETED_CAP:
        _DELETED_JOBS.pop(next(iter(_DELETED_JOBS)), None)


def _tombstoned(jid):
    return jid in _DELETED_JOBS


def _save(job):
    jid = job["id"]
    if _tombstoned(jid):
        return
    with _save_lock(jid):
        # Re-check under the lock: cleanup tombstones first and
        # then drains this lock, so a save that was queued behind
        # an in-flight cleanup still lands nowhere.
        if _tombstoned(jid):
            return
        data = json.dumps(job, ensure_ascii=False, indent=2)
        d = JOBS_DIR / jid
        d.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".job-",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, d / "job.json")
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


class JobCancelled(Exception):
    """Raised inside a worker when the user asked to stop the job."""


# ---------- per-stage job timings (build 213) ----------
# The owner wants to SEE which pipeline stage eats the time
# before deciding on any hardware purchase. Raw marks have
# lived in job["phase_s"] since build 171 (run_job measures
# download / transcribe / translate / tts / retime / finalize
# and the dispatcher stamps the queue wait); build 213 folds
# them into one canonical job["timings"] dict — per-stage
# seconds, queue_s, and an accumulated active-processing
# total_s — shown on the job's own progress/done views and
# averaged for the owner in the Admin panel. Presentation
# only: nothing here feeds a charge, a gate or a decision
# inside a worker.

_TIMING_STAGES = ("download", "transcribe", "translate",
                  "script", "tts", "render")


def _fold_timing(job, key, secs):
    """Fold one raw phase mark into job["timings"]. retime +
    finalize are ONE presentation stage ("render": retime,
    fx/blur, subtitle burn and the encode are a single
    customer-visible step), recomputed from both phase_s
    parts so a mid-render fold never drops the retime half;
    the dispatcher's queue wait becomes queue_s."""
    try:
        secs = round(float(secs), 1)
    except (TypeError, ValueError):
        return
    t = job.setdefault("timings", {})
    if key in _TIMING_STAGES:
        t[key] = secs
    elif key in ("retime", "finalize"):
        ph = job.get("phase_s") or {}
        t["render"] = round(sum(
            float(ph[k]) for k in ("retime", "finalize")
            if isinstance(ph.get(k), (int, float))), 1)
    elif key == "queue_wait":
        t["queue_s"] = secs


def _timings_of(job):
    """The job's timings for display/aggregation: the
    canonical dict when the job has one, else derived
    read-only from a pre-213 job's phase_s marks (stage keys
    + queue only — a legacy total is never fabricated)."""
    t = job.get("timings")
    if t:
        return t
    ph = job.get("phase_s") or {}
    if not ph:
        return {}
    out = {}
    for k in _TIMING_STAGES:
        if isinstance(ph.get(k), (int, float)):
            out[k] = ph[k]
    parts = [ph[k] for k in ("retime", "finalize")
             if isinstance(ph.get(k), (int, float))]
    if parts:
        out["render"] = round(sum(parts), 1)
    if isinstance(ph.get("queue_wait"), (int, float)):
        out["queue_s"] = ph["queue_wait"]
    elif isinstance(job.get("queue_wait_s"), (int, float)):
        out["queue_s"] = job["queue_wait_s"]
    return out


def _timing_start(job):
    """Stamp the start of a worker's active-processing span
    (monotonic — immune to wall-clock jumps)."""
    job["_run_t0"] = time.monotonic()


def _timing_close(job):
    """Close the active span into timings["total_s"],
    ACCUMULATED across a job's workers: a recap job's human
    review park between phase 1 and the render is not machine
    time and must not inflate the hardware-decision number."""
    t0 = job.pop("_run_t0", None)
    if t0 is None:
        return
    try:
        secs = round(time.monotonic() - float(t0), 1)
    except (TypeError, ValueError):
        return
    t = job.setdefault("timings", {})
    t["total_s"] = round(float(t.get("total_s") or 0) + secs, 1)


def _progress_fn(job):
    starts = {}

    def progress(step, state, note="", phase_s=None):
        if job.get("cancel_requested"):
            raise JobCancelled()
        job["updated_at"] = time.time()
        for s in job["steps"]:
            if s["key"] == step:
                s["state"] = state
                if note:
                    s["note"] = note
        job["current"] = step
        if phase_s:
            # Build 171: per-phase seconds measured in run_job
            # — kept on the job (and in job.json) so a slow job
            # can be diagnosed phase by phase. Build 213: the
            # same marks fold into the canonical timings dict
            # the UI + Admin averages read.
            try:
                job.setdefault("phase_s", {}).update(phase_s)
                for k, v in phase_s.items():
                    _fold_timing(job, k, v)
            except Exception:
                pass
            starts.pop(step, None)
        elif state == "running":
            # Build 213: a step announced directly through
            # progress() — the recap "script" step, which has
            # no run_job marks — is timed here, from its own
            # running/done pair.
            starts[step] = time.monotonic()
        elif state == "done" and step in starts:
            secs = round(time.monotonic() - starts.pop(step), 1)
            try:
                job.setdefault("phase_s", {})[step] = secs
                _fold_timing(job, step, secs)
            except Exception:
                pass
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
# Build 215 (owner order 2026-11-16): ONE retention rule for every
# job — 24 hours after a job reaches a FINISHED state (done, error
# or cancelled) its whole folder is deleted: source (downloaded or
# uploaded), intermediates, output video/audio AND the text records
# (job.json with its script / segments / translations). Nothing
# job-related remains. Accounts, the credits ledger, top-ups and
# settings are not job data and are never touched here. Queued /
# running jobs and parked review states (waiting_review /
# waiting_script) are not finished states and are never reaped.
CLEANUP_DELAY = 24 * 3600
CLEANUP_TIMERS = {}


def _job_retention(job):
    """Build 215: the single 24h rule — every finished job, web or
    bot, done, error or cancelled, is kept exactly CLEANUP_DELAY
    after it finished, then deleted in full (files + records).
    (Supersedes the build-180 web/bot split and the build-94
    newest-2-per-account cap, both removed in this build.)"""
    return CLEANUP_DELAY


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
_JOB_QUEUE = []   # (jid, kind) FIFO for the dispatcher; kind "main" or "rerun"
MAX_CONCURRENT_JOBS = 4  # build 180: four heavy jobs at once (YTA 3, 4 cores)
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
            # Build 204 (audit V11): the periodic re-run of the
            # disk recovery pass (its operative run is at startup;
            # in a single-process app disk-only jobs can only
            # appear across a restart, so this is a safety net —
            # and it keeps the dormant sweep below seeing every
            # job that exists, exactly as the old disk-scanning
            # _all_jobs did).
            _scan_disk_jobs()
        except Exception:
            pass
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
            # users.db again — this time CONSISTENTLY: a live copy2
            # of a SQLite file mid-write can capture a torn page.
            # The backup API snapshots through SQLite itself
            # (audit B4); this copy is the one to restore from.
            try:
                import sqlite3 as _sq
                _c1 = _sq.connect(str(ROOT / "users.db"))
                _c2 = _sq.connect(str(dest / "users.db.consistent"))
                _c1.backup(_c2)
                _c2.close()
                _c1.close()
            except Exception:
                pass
            # Payment proof screenshots + the payment QR are money
            # evidence too — they were not backed up at all (B4).
            if (ROOT / "topups").is_dir():
                shutil.copytree(ROOT / "topups", dest / "topups",
                                dirs_exist_ok=True)
            if (ROOT / "payqr.png").exists():
                shutil.copy2(ROOT / "payqr.png", dest / "payqr.png")
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
    return _active_worker_count() > 0


def _active_worker_count():
    """Jobs whose worker thread is actually running right now.
    Queued jobs waiting for a slot do NOT count — worker_started is
    only set by the dispatcher at the moment a worker launches."""
    return sum(1 for j in list(JOBS.values())
               if j.get("worker_started")
               and j.get("status") in ("queued", "running"))


# Build 198: the pipeline's adaptive encode-thread helper reads
# its active-job count from here — a libx264 encode built while
# one job runs alone gets 4 threads; with 2+ active jobs every
# encode is back to the build-193 cap of 2.
set_active_jobs_provider(_active_worker_count)


def _enqueue_job(job, kind="main"):
    """Park a job for the dispatcher (build 180). Submissions are
    never refused for concurrency: they wait here, in order, until
    one of the MAX_CONCURRENT_JOBS worker slots frees up.
    Build 186 (audit fix #7): the enqueue instant is stamped so
    the dispatcher can record how long the job actually waited."""
    job["enqueued_at"] = time.time()
    _JOB_QUEUE.append((job["id"], kind))


def _tg_shrink_for_send(out, dest, budget=47_000_000):
    """Re-encode an oversized output so the bot can send it: fit into
    a 480p-class box and cap the bitrate so the file lands under
    `budget` bytes for its duration. Returns dest on success (and the
    file really is under the bot cap), else None."""
    from pipeline import telegram_send as tg
    try:
        dur = probe_duration(out)
        if dur <= 0:
            return None
        from pipeline.common import probe_streams
        vw = vh = None
        for s in probe_streams(out):
            if s.get("codec_type") == "video":
                vw, vh = int(s["width"]), int(s["height"])
                break
        if not vw:
            return None
        # 480p-class box, orientation-aware, exact dims computed here:
        # expression sizes AND force_original_aspect_ratio both make
        # this ffmpeg build's filter setup fail, plain scale=W:H works.
        box_w, box_h = (854, 480) if vw >= vh else (480, 854)
        k = min(box_w / vw, box_h / vh, 1.0)
        tw = max(2, int(vw * k) // 2 * 2)
        th = max(2, int(vh * k) // 2 * 2)
        vf = f"scale={tw}:{th}"
        total_k = budget * 8 / dur / 1000.0 * 0.94
        video_k = int(max(220, total_k - 96))
        # Build 193 capped this encode's CPU ("-threads 2") so a
        # small-copy/shrink pass cannot starve the web server
        # sharing the box (same cap in retime/burn/fx). Build 198:
        # the cap is adaptive (common.encode_threads) — 4 threads
        # when no other job is active, still 2 with 2+ active.
        sh(["ffmpeg", "-y", "-v", "error", "-i", str(out), "-vf", vf,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
            "-maxrate", f"{video_k}k", "-bufsize", f"{2 * video_k}k",
            "-c:a", "aac", "-b:a", "96k",
            "-threads", str(encode_threads()),
            str(dest)], timeout=1800)
        if dest.exists() and dest.stat().st_size <= tg.MAX_BYTES:
            return dest
    except Exception:
        pass
    return None


def _small_budget(dur):
    """Byte budget for a job's compact download copy: ~1 Mbit/s of
    video (~7.5 MB per minute) — several times smaller than the HD
    master, so a phone download finishes in a fraction of the time,
    while staying watchable. Floored so very short clips still get
    reasonable quality; capped at the Telegram shrink's budget."""
    if not dur or dur <= 0:
        return 8_000_000
    return max(2_500_000, min(47_000_000, int(dur * 125_000)))


def _ensure_small_copy(job):
    """Build 179: a finished job's compact copy, output_small.mp4,
    made with the Telegram shrink's exact recipe (480p-class box,
    x264 ultrafast/crf 26, duration-capped bitrate, AAC 96k) but
    budgeted for download speed instead of the bot's 50 MB cap.
    The done-state's main download button serves this copy; the
    full master stays one tap away (?v=full). Lives in the job
    dir, so the single 24h retention rule (a whole-dir delete)
    covers it — no orphans.

    Build 198: LAZY — nothing calls this at finish time any more
    (build 186 already had it off the _finish path, in a
    background thread; now it is not made at all until asked).
    The only caller is _small_on_demand, on the first ?v=small
    download/stream request. Every exit that produces NO copy
    stamps job["small_na"] so later taps serve the full master
    immediately instead of re-running a doomed encode; _finish
    clears the stamp for a fresh render. Failure is non-fatal,
    exactly as before: the full file always remains downloadable."""
    jdir = JOBS_DIR / job["id"]
    out = jdir / "output.mp4"
    small = jdir / "output_small.mp4"
    try:
        job.pop("small", None)
        if not out.exists():
            job["small_na"] = True
            return
        full_size = out.stat().st_size
        job["output_size"] = full_size
        if small.exists():
            small.unlink()
        got = _tg_shrink_for_send(
            out, small, budget=_small_budget(probe_duration(out)))
        if not got or not small.exists():
            job["small_na"] = True
            return
        ssize = small.stat().st_size
        # A "compact" copy that barely shrinks the master (short or
        # low-res sources) is pointless weight: drop it; the route
        # then serves the full download for this job's taps.
        if ssize >= full_size * 0.9:
            small.unlink(missing_ok=True)
            job["small_na"] = True
            return
        w = h = None
        from pipeline.common import probe_streams
        for s in probe_streams(small):
            if s.get("codec_type") == "video":
                w, h = int(s["width"]), int(s["height"])
                break
        job["small"] = {"size": ssize, "w": w, "h": h}
        job.pop("small_na", None)
    except Exception:
        job["small_na"] = True
        try:
            small.unlink(missing_ok=True)
        except Exception:
            pass


_SMALL_LOCKS = {}
_SMALL_LOCKS_GUARD = threading.Lock()


def _small_lock(jid):
    with _SMALL_LOCKS_GUARD:
        return _SMALL_LOCKS.setdefault(jid, threading.Lock())


def _small_on_demand(jid):
    """Build 198: generate the compact copy on FIRST demand (the
    ?v=small download/stream routes), synchronously under the
    per-job lock — the copy is a 480p ultrafast encode, seconds
    even for a long video, and the lock means concurrent first
    taps share ONE generation instead of racing two encodes.
    Never raises: any failure leaves the job stamped small_na
    and the routes fall back to the full master, so the main
    download can never be broken by the copy. The generation's
    seconds are recorded in the job's timings, as the build-186
    background thread recorded them."""
    job = JOBS.get(jid)
    if job is None or job.get("status") != "done" \
            or job.get("small_na"):
        return
    with _small_lock(jid):
        small = JOBS_DIR / jid / "output_small.mp4"
        if small.exists() or job.get("small_na"):
            return
        t0 = time.time()
        try:
            _ensure_small_copy(job)
        except Exception:
            job["small_na"] = True
        try:
            secs = round(time.time() - t0, 1)
            job["small_copy_s"] = secs
            job.setdefault("phase_s", {})["small_copy"] = secs
        except Exception:
            pass
        # Guarded save, as the old background thread did: the job
        # dir may have been reaped meanwhile (24h retention) —
        # never resurrect a deleted job.
        try:
            if (JOBS_DIR / jid).exists() and JOBS.get(jid) is job:
                _save(job)
        except Exception:
            pass


def _deliver_video_core(job, chat_id, record, cap_full, cap_shrunk,
                        notify_failure):
    """The Telegram delivery itself — one implementation shared by
    bot jobs (_tg_deliver_async) and web-job auto-send (build 188,
    _web_tg_autosend), so the rules can never drift apart:

    * LOCAL Bot API (build 184): the full file goes as-is up to
      the local 2 GB-class cap; if that send fails for any reason,
      the cloud behaviour below takes over, so a broken local
      server can never strand a finished video.
    * Cloud: a file up to the 50 MB bot cap goes directly; a
      larger one goes as a 480p-class compact copy made by
      _tg_shrink_for_send, plus a message pointing at the
      full-quality file in the web app.

    `record(d)` stores each state dict under the caller's own job
    key (the caller merges in its "to"). With notify_failure (bot
    jobs) the chat also gets a plain message when the video cannot
    be sent at all — for a bot customer the chat is the only place
    they would look. Web auto-send passes False: that customer is
    already looking at the web app, so a failure is only recorded
    on the job (and the manual ✉️ button stays available)."""
    from pipeline import telegram_send as tg
    out = JOBS_DIR / job["id"] / "output.mp4"
    try:
        if not out.exists():
            return
        size = out.stat().st_size
        if tg.is_local() and tg.MAX_BYTES < size <= tg.LOCAL_MAX_BYTES:
            try:
                record({"state": "sending"})
                _save(job)
                tg.send_video(chat_id, out, caption=cap_full)
                record({"state": "sent", "direct": True})
                _save(job)
                return
            except Exception:
                pass  # compact copy + web link below, as on cloud
        if size > tg.MAX_BYTES:
            small = _tg_shrink_for_send(
                out, out.with_name("output_tg.mp4"))
            if small:
                record({"state": "sending"})
                _save(job)
                tg.send_video(chat_id, small, caption=cap_shrunk)
                tg.send_message(
                    chat_id,
                    "🌐 quality အပြည့် ကို ဒီမှာ ယူနိုင်ပါတယ်: "
                    + APP_LINK
                    + "\n(Jobs စာရင်းထဲမှာ ၂၄ နာရီ အတွင်း ယူပေးပါ)")
                record({"state": "sent", "shrunk": True})
            else:
                if notify_failure:
                    tg.send_message(
                        chat_id,
                        f"✅ video ပြီးပါပြီ — ဒါပေမဲ့ ဖိုင် {size / 1e6:.0f}MB "
                        "ဖြစ်နေလို့ Telegram bot ကနေ ပို့လို့ မရပါ (အများဆုံး 50MB ပါ)\n"
                        "🌐 web app မှာ ယူနိုင်ပါတယ်: " + APP_LINK
                        + "\n(Jobs စာရင်းထဲမှာ ၂၄ နာရီ အတွင်း ယူပေးပါ) 🙏")
                record({"state": "failed", "error": "file > 50MB"})
        else:
            record({"state": "sending"})
            _save(job)
            tg.send_video(chat_id, out, caption=cap_full)
            record({"state": "sent"})
    except Exception as e:
        record({"state": "failed", "error": str(e)[:200]})
        if notify_failure:
            # A failed delivery must not be silent: tell the
            # customer where else to get the video (same promise
            # as oversized).
            try:
                tg.send_message(
                    chat_id,
                    "🙏 တောင်းပန်ပါတယ် — video ပို့လို့ အဆင်မပြေ ဖြစ်သွားပါတယ်\n"
                    "🌐 web app မှာ ယူနိုင်ပါတယ်: " + APP_LINK
                    + "\n(Jobs စာရင်းထဲမှာ ၂၄ နာရီ အတွင်း ယူပေးပါ)")
            except Exception:
                pass
    _save(job)


def _tg_deliver_async(job):
    """A bot-submitted job finished: send the video back to its
    chat. Over the bot API's 50 MB cap the user still gets the
    video: a 480p-class compressed copy goes to the chat and the
    full-quality file stays in their web jobs list (bot jobs are
    kept 24h). The delivery itself lives in _deliver_video_core,
    shared with web-job auto-send (build 188)."""
    chat_id = job.get("tg_chat_id")
    if not chat_id:
        return

    def record(d):
        job["tg"] = {**d, "to": job.get("tg_from")}

    def go():
        _deliver_video_core(
            job, chat_id, record,
            cap_full="🎬 မြန်မာ ဘာသာပြန် ဗီဒီယို\n"
                     + (job.get("url") or "")
                     + "\n🙏 ကျေးဇူးတင်ပါတယ်",
            cap_shrunk="🎬 မြန်မာ ဘာသာပြန် ဗီဒီယို\n"
                       "(ဖိုင် ကြီးလို့ Telegram အတွက် ချုံ့ ထားပါတယ် — "
                       "quality အပြည့် ကို web app မှာ ယူနိုင်ပါတယ် 🙏)",
            notify_failure=True)

    threading.Thread(target=go, daemon=True).start()


def _web_tg_autosend(job):
    """Build 188 (owner order 2026-11-15): a finished WEB job is
    delivered to the account's linked Telegram chat automatically
    — "web job ပြီးတာနဲ့ TG ကို အလိုအလျောက် ပို့ပေး" — so the
    customer finds the video in their Telegram without tapping
    the ✉️ button. Same delivery core and rules as bot jobs
    (_deliver_video_core), with a caption naming it as a web job
    result. Boundaries:

    * Bot-origin jobs never come here: job["tg_chat_id"] is the
      origin marker set at bot submission, and _tg_deliver_async
      already delivered those to their chat — a second send
      would be a duplicate.
    * The target is ONLY the account's own linked chat (the
      build-159 rule); no linked chat → nothing happens and the
      web download stands as today.
    * Fires after the done flip and the charge, fire-and-forget:
      a failed send is recorded on the job (job["auto_tg"]) and
      can never fail or re-charge the job; the manual ✉️ button
      remains available.
    * Exactly-once: the job["auto_tg"] record is set and saved
      BEFORE the thread fires, so a re-finish (a review re-render
      calls _finish again) or a restart can never send twice."""
    if job.get("tg_chat_id") or job.get("auto_tg"):
        return
    uid = job.get("user_id")
    if not uid:
        return
    try:
        user = accounts.get_user(uid)
    except Exception:
        return
    chat_id = (user or {}).get("tg_chat_id")
    if not chat_id:
        return
    chat_id = int(chat_id)
    job["auto_tg"] = {"state": "sending", "to": chat_id}
    _save(job)

    def record(d):
        job["auto_tg"] = {**d, "to": chat_id}

    def go():
        _deliver_video_core(
            job, chat_id, record,
            cap_full="🎬 မြန်မာ ဘာသာပြန် ဗီဒီယို "
                     "(web app ကနေ လုပ်ထားတာ)\n"
                     + (job.get("url") or "")
                     + "\n🙏 ကျေးဇူးတင်ပါတယ်",
            cap_shrunk="🎬 မြန်မာ ဘာသာပြန် ဗီဒီယို "
                       "(web app ကနေ လုပ်ထားတာ)\n"
                       "(ဖိုင် ကြီးလို့ Telegram အတွက် ချုံ့ ထားပါတယ် — "
                       "quality အပြည့် ကို web app မှာ ယူနိုင်ပါတယ် 🙏)",
            notify_failure=False)

    threading.Thread(target=go, daemon=True).start()


def _tg_error_async(job):
    chat_id = job.get("tg_chat_id")
    if not chat_id:
        return

    def go():
        try:
            from pipeline import telegram_send as tg
            tg.send_message(chat_id, "🙏 တောင်းပန်ပါတယ် — ဒီ video ကို လုပ်လို့ မရပါ ဖြစ်သွားပါတယ်\nအကြောင်း: "
                            + str(job.get("error") or "")[:300] + "\nlink ကို ပြန်စစ်ပြီး နောက်တစ်ခါ ပို့ကြည့်ပေးပါ 🙏")
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
            tg.send_message(chat_id, "⏳ ရှင့် အရင် video လုပ်နေဆဲ ဖြစ်ပါတယ် — "
                                     "ပြီးသွားမှ နောက်တစ်ခု ပို့ပေးပါ 🙏")
            return
        if (vid.get("file_size") or 0) > tg.download_cap():
            cap_mb = tg.download_cap() // 1_000_000
            tg.send_message(chat_id, f"⚠️ တောင်းပန်ပါတယ် — ဒီ file က {cap_mb}MB ထက် ကြီးနေလို့ bot က လက်ခံလို့ မရပါ။ "
                                     "video ရဲ့ link ကို ပို့ပေးပါ 🙏")
            return
        frm = msg.get("from") or {}
        _TG_FLOW[chat_id] = {
            "file_id": vid["file_id"], "step": "prepare",
            "from": frm.get("username") or frm.get("first_name"),
            "ts": time.time()}
        tg.send_message(chat_id, "⏳ file ကို ယူနေပါတယ် — ခဏ စောင့်ပေးပါ။ ဝါးမယ့် နေရာ "
                                 "ရွေးဖို့ ပုံ ပို့ပေးပါမယ်")
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
            if _recovery_allowed(chat_id):
                # Lockout hint (build 158): if an admin account is
                # disabled, tell the owner the recovery phrase.
                try:
                    if any(u["disabled"] for u in accounts.list_admins()):
                        tg.send_message(
                            chat_id, "⚠️ Admin account ပိတ်နေတာ တွေ့ပါတယ် — "
                                     "ပြန်ဖွင့်ဖို့ «" + _RECOVERY_PHRASE + "» လို့ "
                                     "ဒီမှာ ပို့ပေးပါ")
                except Exception:
                    pass
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
                        "🎬 ဘာသာပြန် လိုတဲ့ video ရဲ့ link ကို ပို့ပေးပါ (YouTube / TikTok) 🙏")
        return
    mine = [j for j in JOBS.values()
            if j.get("tg_chat_id") == chat_id
            and j.get("status") in ("queued", "running")]
    if mine:
        tg.send_message(chat_id,
                        "⏳ ရှင့် အရင် video လုပ်နေဆဲ ဖြစ်ပါတယ် — ပြီးသွားမှ နောက် link ပို့ပေးပါ 🙏")
        return
    frm = msg.get("from") or {}
    _TG_FLOW[chat_id] = {
        "url": m.group(0), "step": "prepare",
        "from": frm.get("username") or frm.get("first_name"),
        "ts": time.time()}
    tg.send_message(chat_id,
                    "⏳ video ကို ယူနေပါတယ် — ခဏ စောင့်ပေးပါ။ ဝါးမယ့် နေရာ ရွေးဖို့ ပုံ ပို့ပေးပါမယ်")
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
              "\n📲 app အဖြစ် ထည့်ထားနိုင်ပါတယ်: link ကို browser မှာ ဖွင့်ပြီး "
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


# ---------- Google sign-in + daily job limit (build 180) ----------

# The owner's fixed Google identity for THIS app (same fixed-owner
# pattern as the MLBB platform): a Google sign-in presenting this
# email, verified by Google, lands in an admin account. No other
# Google account can ever gain admin through Google sign-in.
FIXED_ADMIN_EMAIL = "nnominalhuman@gmail.com"


def _env_value(key):
    """Read one KEY=value from the project .env, live (admin edits
    take effect without a restart), env var as fallback."""
    p = ROOT / ".env"
    if p.exists():
        for ln in p.read_text(encoding="utf-8").splitlines():
            if ln.startswith(key + "="):
                return ln.split("=", 1)[1].strip()
    return os.environ.get(key, "")


def _google_client_id():
    return _env_value("GOOGLE_CLIENT_ID")


_YANGON_TZ = timezone(timedelta(hours=6, minutes=30))  # no DST, ever


def _yangon_day(ts):
    return datetime.fromtimestamp(float(ts), _YANGON_TZ).date()


def _daily_job_limit():
    """Admin setting DAILY_JOB_LIMIT: max videos per account per
    day, 0 = unlimited (the default). Read live like BOT_OPEN."""
    try:
        return max(0, int(_env_value("DAILY_JOB_LIMIT") or 0))
    except (TypeError, ValueError):
        return 0


# Build 231 pricing: 1 credit = 5 kyat. PACKAGES maps a
# one-time kyat payment to (total credits credited on approval,
# bonus credits inside that total): the 10,000+ tiers carry a
# +5%/+10%/+15% bonus over the straight 5-kyat rate. Credits
# NEVER expire — there is no expiry anywhere in the ledger.
PACKAGES = {5000: (1000, 0), 10000: (2100, 100),
            25000: (5500, 500), 50000: (11500, 1500)}


def _standard_credits_per_min():
    """Admin setting STANDARD_CREDITS_PER_MIN: the per-started-
    minute rate for standard voices (Thiha/Nilar/clones) —
    build 231's rate card sets it to 40 (= 200 kyat at the
    5-kyat credit). Read live like DAILY_JOB_LIMIT."""
    try:
        return max(1, min(1000, int(
            _env_value("STANDARD_CREDITS_PER_MIN") or 40)))
    except (TypeError, ValueError):
        return 40


def _commentary_credits_per_min():
    """Admin setting COMMENTARY_CREDITS_PER_MIN: the standard
    per-started-minute rate for Commentary jobs (build 217),
    default 2 (= 300 kyat/min at the 150-kyat credit — inside
    the owner's 300-400 kyat/min target band, against Burma
    Voice's ~1,800). Read live like DAILY_JOB_LIMIT. Premium
    ElevenLabs voices on a commentary job bill the premium
    rate + 1 (see _credits_per_min_for)."""
    try:
        return max(1, min(1000, int(
            _env_value("COMMENTARY_CREDITS_PER_MIN") or 80)))
    except (TypeError, ValueError):
        return 80


def _commentary_gemini_credits_per_min():
    """Admin setting COMMENTARY_GEMINI_CREDITS_PER_MIN: the
    per-started-minute rate for Commentary with a Gemini voice
    — build 231's card sets it to 120 (= 600 kyat). An explicit
    per-tier rate (build 229's std x Gemini-rate formula cannot
    express the card in absolute new units)."""
    try:
        return max(1, min(1000, int(_env_value(
            "COMMENTARY_GEMINI_CREDITS_PER_MIN") or 120)))
    except (TypeError, ValueError):
        return 120


def _commentary_elevenlabs_credits_per_min():
    """Admin setting COMMENTARY_ELEVENLABS_CREDITS_PER_MIN:
    the per-started-minute rate for Commentary with an
    ElevenLabs voice — build 231's card sets it to 160
    (= 800 kyat). Explicit per-tier rate, like the Gemini
    tier above."""
    try:
        return max(1, min(1000, int(_env_value(
            "COMMENTARY_ELEVENLABS_CREDITS_PER_MIN") or 160)))
    except (TypeError, ValueError):
        return 160


def _captions_credits_per_min():
    """Admin setting CAPTIONS_CREDITS_PER_MIN: the per-started-
    minute rate for Captions-only jobs (build 219) — a FRACTION,
    default 0.5, because the product's price is half the dub
    rate: credits = ceil(started minutes x this rate), minimum
    1 (1-2 min = 1 credit, 3-4 min = 2). Read live like
    COMMENTARY_CREDITS_PER_MIN. The quote, the upload balance
    pre-check and the final charge all go through
    _credits_for_minutes, so the three always agree exactly."""
    try:
        return max(1, min(1000, int(
            _env_value("CAPTIONS_CREDITS_PER_MIN") or 20)))
    except (TypeError, ValueError):
        return 20


def _download_credits_per_min():
    """Admin setting DOWNLOAD_CREDITS_PER_MIN: the per-started-
    minute rate for the download-only product (build 234),
    default 10 (= 50 kyat/min at the build-231 rate card —
    the owner's price, 2026-11-20). Read live like
    CAPTIONS_CREDITS_PER_MIN. The quote and the charge both
    go through _credits_for_minutes, so they always agree."""
    try:
        return max(1, min(1000, int(
            _env_value("DOWNLOAD_CREDITS_PER_MIN") or 10)))
    except (TypeError, ValueError):
        return 10


def _referral_reward_credits():
    """Admin setting REFERRAL_REWARD_CREDITS: the one-time
    credit reward an inviter earns when a referred account
    completes its first charged job (build 221), default 2.
    Read live like CAPTIONS_CREDITS_PER_MIN. 0 switches the
    payout off (referral links still record who invited whom;
    the one-time flag is only consumed by an actual payout)."""
    try:
        return max(0, min(1000, int(
            _env_value("REFERRAL_REWARD_CREDITS") or 60)))
    except (TypeError, ValueError):
        return 60


def _maybe_referral_reward(job):
    """Build 221: after a job's success charge actually took
    credits (charged_minutes > 0), a referred account's FIRST
    such job pays its inviter the referral reward — exactly
    once (accounts.pay_referral_reward flips a persisted flag
    atomically, so re-renders/restarts can't double-pay).
    Called from both charge points (_charge_for_job for every
    video product, _tts_charge for the studio). Failed and
    cancelled jobs never reach a charge; admin/anonymous jobs
    charge 0 — neither triggers anything. Best-effort: a
    referral hiccup must never break a finished job."""
    try:
        uid = job.get("user_id")
        if not uid or not (job.get("charged_minutes") or 0):
            return
        amount = _referral_reward_credits()
        if amount > 0:
            accounts.pay_referral_reward(uid, amount)
    except Exception:
        pass


def _account_daily_count(user_id):
    """Videos this account STARTED today (Asia/Yangon), web and
    Telegram combined — both channels stamp the same user_id on
    their jobs. Failed/cancelled jobs don't count: the customer
    never got a video from them."""
    today = _yangon_day(time.time())
    return sum(1 for j in _all_jobs()
               if j.get("user_id") == user_id
               and j.get("status") in ("queued", "running",
                                       "waiting_review", "done")
               and j.get("created_at")
               and _yangon_day(j["created_at"]) == today)


def _daily_limit_message(user):
    """None when this account may start another video today, else
    the polite Burmese refusal naming the limit. Admins exempt."""
    if not user or user.get("role") == "admin":
        return None
    limit = _daily_job_limit()
    if limit <= 0:
        return None
    if _account_daily_count(user["id"]) >= limit:
        return (f"🚫 တောင်းပန်ပါတယ် — တစ်ရက် အများဆုံး video {limit} ပုဒ် "
                "ပဲ ထုတ်လို့ရပါတယ်၊ ဒီနေ့ အတွက် ပြည့်သွားပါပြီ။ "
                "မနက်ဖြန် ပြန် ကြိုးစားပေးပါ 🙏")
    return None


def _setup_gate_message(user):
    """Build 187: None when this account may start jobs, else the
    polite Burmese refusal. A Google-born account must finish the
    one-time username+password setup first. The check lives at JOB
    START — web create and the bot flow's final step both call it,
    next to the daily-limit check — so every submission route
    inherits it and a refusal always lands before any charge.
    Admins are never gated (ensure_admin also clears the flag at
    promotion, so the owner cannot be locked out either way)."""
    if not user or user.get("role") == "admin":
        return None
    if user.get("needs_setup"):
        return ("📝 account ပြင်ဆင်ရန် လိုပါသေးတယ် — Google နဲ့ "
                "ဝင်ထားတာမို့ username နဲ့ password တစ်ခါ "
                "သတ်မှတ်ပေးပါ (ခဏလေးပဲ ကြာပါတယ်)၊ ပြီးမှ video "
                "ထုတ်လို့ရပါမယ် 🙏")
    return None


def _may_submit(chat_id):
    from pipeline import telegram_send as tg
    if tg.is_admin(chat_id):
        return True
    return _bot_open() and bool(accounts.user_for_tg(chat_id))


def _deny_submit(chat_id):
    from pipeline import telegram_send as tg
    if _bot_open():
        tg.send_message(chat_id, "🙏 video ထုတ်ဖို့ အရင် 👤 Username ချိတ်ပေးပါ (အောက်က Menu ထဲက «👤 Username ချိတ်» ကို နှိပ်ပါ)"
                        + _LINK_HINT)
    else:
        tg.send_message(chat_id, "🔒 တောင်းပန်ပါတယ် — အခုတော့ ဆိုင် မဖွင့်သေးပါ။ ဖွင့်တဲ့ အခါ အသိပေးပါမယ် 🙏")


# ------------------------------------------------------------------
# Lockout recovery phrase (build 160). The Burmese words are built
# from explicit codepoints on purpose: the build-158 literals had
# the tone marks in the wrong order and dropped a consonant, so the
# owner's correctly typed phrase could never match and the bot fell
# through to the job flow (proven live 2026-11-12 00:25). Matching
# is tolerant: zero-width chars and quote marks are stripped, and a
# tone-mark-free skeleton accepts keyboard ordering variants; the
# legacy build-158 spelling stays accepted for copy-paste.
_RECOVERY_PHRASE = ("Admin " + "\u1015\u103c\u1014\u103a" + " "
                    + "\u1016\u103d\u1004\u103a\u1010\u1037")
_RECOVERY_LEGACY = ("Admin " + "\u1015\u103c\u1014\u103a" + " "
                    + "\u1016\u103d\u1004\u1037\u103a")


def _phrase_norm(s):
    s = s or ""
    for ch in ("\u200b", "\u200c", "\u00ab", "\u00bb", "\u201c",
               "\u201d", '"', "'"):
        s = s.replace(ch, "")
    return " ".join(s.split()).strip().lower()


def _phrase_skeleton(s):
    s = _phrase_norm(s)
    for ch in ("\u1037", "\u103a", "\u1039"):
        s = s.replace(ch, "")
    return s.replace(" ", "")


_RECOVERY_SET = {_phrase_norm(_RECOVERY_PHRASE),
                 _phrase_norm("\U0001F513 " + _RECOVERY_PHRASE),
                 _phrase_norm(_RECOVERY_LEGACY),
                 _phrase_norm("\U0001F513 " + _RECOVERY_LEGACY),
                 "/recover_admin"}
_RECOVERY_SKEL = {_phrase_skeleton(_RECOVERY_PHRASE),
                  _phrase_skeleton(_RECOVERY_LEGACY)}


def _is_recovery_text(text):
    return (_phrase_norm(text) in _RECOVERY_SET
            or _phrase_skeleton(text) in _RECOVERY_SKEL)


def _recovery_allowed(chat_id):
    """Owner-only recovery: the fixed Telegram chat, or a chat
    linked to an admin account (linking itself required that
    account's web login, so the chat is the account holder's)."""
    from pipeline import telegram_send as tg
    if tg.is_admin(chat_id):
        return True
    acct = accounts.user_for_tg(chat_id)
    return bool(acct and acct.get("role") == "admin")


def _tg_menu_step(msg, chat_id, text):
    """Bot account menu + the link / top-up conversations. Returns
    True when the message was consumed by one of them. These are
    ACCOUNT functions (open to anyone who linked an account); job
    submission stays behind the admin lock in the caller."""
    from pipeline import telegram_send as tg
    if _is_recovery_text(text) and _recovery_allowed(chat_id):
        # Lockout recovery (build 158): ONLY the owner's fixed
        # Telegram chat can do this — that chat id IS the owner's
        # identity, no password involved. Re-enables every disabled
        # admin account so the owner can log straight back in with
        # the admin password they know.
        fixed = [u for u in accounts.list_admins() if u["disabled"]]
        n = sum(1 for u in fixed
                if accounts.set_disabled(u["id"], False))
        if n:
            tg.send_message(
                chat_id, f"✅ Admin account {n} ခု ပြန်ဖွင့်ပြီးပါပြီ — "
                         "web app မှာ မူလ password နဲ့ တန်း login "
                         "ဝင်နိုင်ပါပြီ 🙏")
        else:
            tg.send_message(
                chat_id, "ပြန်ဖွင့်စရာ ပိတ်နေတဲ့ admin account မရှိပါ — "
                         "အားလုံး ပုံမှန် ဖြစ်နေပါတယ်")
        return True
    flow = _TG_FLOW.get(chat_id)
    acct = accounts.user_for_tg(chat_id)
    photos = msg.get("photo")
    if flow and flow.get("step") == "logo_img":
        file_id, ext = None, ".jpg"
        if photos:
            file_id = photos[-1]["file_id"]
        else:
            doc = msg.get("document") or {}
            if (doc.get("mime_type") or "").startswith("image/"):
                file_id = doc.get("file_id")
                e = Path(doc.get("file_name") or "").suffix.lower()
                if e in (".png", ".jpg", ".jpeg", ".webp"):
                    ext = e
        if file_id:
            dest = UPLOADS_DIR / f"{uuid.uuid4().hex[:12]}{ext}"
            try:
                tg.download_file(file_id, dest)
            except Exception:
                tg.send_message(chat_id, "⚠️ တောင်းပန်ပါတယ် — ပုံ ယူလို့ မရပါ။ "
                                         "နောက်တစ်ခါ ပြန်ပို့ပေးပါ 🙏")
                return True
            flow["logo_file"] = str(dest)
            flow["step"] = "logo_pos"
            tg.send_message(chat_id, "Logo ကို ဘယ်နေရာ ထားမလဲ",
                            buttons=[[("↖ အပေါ်-ဘယ်", "g:tl"),
                                      ("↗ အပေါ်-ညာ", "g:tr")],
                                     [("↙ အောက်-ဘယ်", "g:bl"),
                                      ("↘ အောက်-ညာ", "g:br")]])
            return True
    if photos and flow and flow.get("step") == "topup_proof" and acct:
        amount = flow.get("amount") or 0
        tid = accounts.create_topup(
            acct["id"], amount, None,
            package_kyat=flow.get("package"))
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
        if flow.get("package") and \
                flow["package"] in PACKAGES:
            cap += (f"\n📦 Package {flow['package']:,} ကျပ် "
                    f"(bonus +"
                    f"{PACKAGES[flow['package']][1]:,})")
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
        tg.send_message(chat_id, "✅ တောင်းဆိုချက် ပို့ပြီးပါပြီ — "
                                 "စစ်ဆေးပြီးတာနဲ့ credit ဝင်လာပါမယ်။ ကျေးဇူးတင်ပါတယ် 🙏")
        return True
    if not text:
        return False
    is_menu = text in ("🎬 Video ထုတ်မယ်", "💰 Credit ဖြည့်",
                       "💳 လက်ကျန်", "👤 Username ချိတ်")
    if flow and flow.get("step") == "link" and not is_menu:
        fails = _LINK_FAILS.get(chat_id)
        if fails and fails[1] > time.time():
            tg.send_message(chat_id, "🔒 code မှား ထည့်တာ များလို့ ခဏ ပိတ်ထားပါတယ် — "
                                     "၃၀ မိနစ် စောင့်ပြီးမှ ပြန် ကြိုးစားပေးပါ 🙏")
            return True
        entry = _LINK_CODES.pop(text.strip(), None)
        _TG_FLOW.pop(chat_id, None)
        if not (entry and entry[1] > time.time()):
            count = (fails[0] + 1) if fails else 1
            until = time.time() + 1800 if count >= 5 else 0
            _LINK_FAILS[chat_id] = (count, until)
        if entry and entry[1] > time.time():
            _LINK_FAILS.pop(chat_id, None)
            if not accounts.link_tg(entry[0], chat_id):
                tg.send_message(
                    chat_id, "⚠️ တောင်းပန်ပါတယ် — ဒီ Telegram က အခြား account "
                             "တစ်ခု နဲ့ ချိတ်ထားပြီးသား ဖြစ်နေပါတယ်။ အရင် account "
                             "က ဖြုတ်ပြီးမှ ပြန် ကြိုးစားပေးပါ 🙏")
                return True
            user = accounts.get_user(entry[0])
            tg.send_message(
                chat_id, f"✅ ချိတ်ဆက် ပြီးပါပြီ — 👤 {user['username']} "
                         f"(💳 {user['credits']} credits)\nအခု video link ပို့ပြီး သုံးနိုင်ပါပြီ 🙏")
        else:
            tg.send_message(chat_id, "❌ တောင်းပန်ပါတယ် — code မမှန်ပါ (သို့) သက်တမ်း ကုန်သွားပါပြီ။ "
                                     "web app ရဲ့ account card မှာ code အသစ် ထုတ်ပြီး ပြန်ပို့ပေးပါ 🙏")
        return True
    if flow and flow.get("step") == "topup_amount" and not is_menu:
        try:
            amount = int(text.strip())
            assert 1 <= amount <= 100000
        except Exception:
            tg.send_message(chat_id, "🙏 နံပါတ် ပဲ ပို့ပေးပါ (ဥပမာ — 1000)")
            return True
        flow["amount"] = amount
        flow["step"] = "topup_proof"
        tg.send_message(chat_id, "📸 ငွေ လွှဲပြီးရင် screenshot ပို့ပေးပါ 🙏")
        return True
    if text == "🎬 Video ထုတ်မယ်":
        tg.send_message(chat_id, "🎬 video link (YouTube / TikTok) ကို ပို့ပေးပါ — သို့ "
                                 "video file ကို ဒီမှာ တင်ပေးပါ 🙏")
        return True
    if text == "💳 လက်ကျန်":
        if acct:
            msg = (f"💳 👤 {acct['username']} — "
                   f"လက်ကျန် {acct['credits']} credits ရှိပါတယ်")
            if acct.get("referral_code"):
                # Build 221: the bot balance view also carries
                # the account's own referral code (read-only).
                msg += (f"\n🎁 သင့် referral code: "
                        f"{acct['referral_code']}\n"
                        "သူငယ်ချင်းတွေ ဒီ code နဲ့ account "
                        "ဖွင့်ပြီး ပထမဆုံး အလုပ် ပြီးတာနဲ့ "
                        "သင် credit ဆု ရပါမယ်")
            tg.send_message(chat_id, msg)
        else:
            tg.send_message(chat_id, "🙏 အရင် 👤 Username ချိတ်ပေးပါ — web "
                                     "app ရဲ့ account card မှာ code ထုတ်ပြီး ဒီမှာ ပို့ရပါမယ်" + _LINK_HINT)
        return True
    if text == "👤 Username ချိတ်":
        if acct:
            tg.send_message(chat_id, f"✅ 👤 {acct['username']} နဲ့ "
                                     "ချိတ်ပြီးသား ဖြစ်ပါတယ်")
        else:
            _TG_FLOW[chat_id] = {"step": "link", "ts": time.time()}
            tg.send_message(chat_id, "🔑 web app ရဲ့ 👤 account card မှာ "
                                     "«TG ချိတ်ရန် code» ထုတ်ပြီး အဲဒီ "
                                     "code ကို ဒီမှာ ပို့ပေးပါ" + _LINK_HINT)
        return True
    if text == "💰 Credit ဖြည့်":
        if not acct:
            tg.send_message(chat_id, "🙏 credit ဖြည့်ဖို့ အရင် 👤 Username ချိတ်ပေးပါ"
                                     "" + _LINK_HINT)
            return True
        _TG_FLOW[chat_id] = {"step": "topup_amount", "ts": time.time()}
        info = _pay_info()
        # Build 231: package buttons ride on the prompt — a
        # tap fills the amount with the package's TOTAL
        # credits (base + bonus) via the p: callback; typing
        # a number stays free-form. Credits never expire and
        # the prompt says so.
        tg.send_message(chat_id, "💰 ငွေ လွှဲရန်:\n"
                        + (info or "နံပါတ် ကို admin ဆီ မေးပေးပါ")
                        + "\n\n📦 Package ရွေးပါ (bonus ပါပြီးသား) — "
                          "သို့ ဘယ်နှစ် credits ဖြည့်ချင်လဲ နံပါတ် ပဲ "
                          "ပို့ပေးပါ (ဥပမာ 1000)\n"
                          "💳 credit က သက်တမ်း လုံးဝ မကုန်ပါ",
                        buttons=[[("📦 5,000 ကျပ် → 1,000 cr",
                                   "p:5000"),
                                  ("📦 10,000 → 2,100 cr",
                                   "p:10000")],
                                 [("📦 25,000 → 5,500 cr",
                                   "p:25000"),
                                  ("📦 50,000 → 11,500 cr",
                                   "p:50000")]])
        qr = ROOT / "payqr.png"
        if qr.exists():
            try:
                tg.send_photo(chat_id, qr, caption="📸 QR ကို scan ပြီး လွှဲပေးပါ 🙏")
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
            if not _src_valid(src):
                src.unlink(missing_ok=True)
                tg.download_file(flow["file_id"], src)
        else:
            if not _public_url_ok(flow["url"]):
                raise RuntimeError("ဒီ လင့်ခ် ကို သုံးလို့ မရပါ")
            # Build 203 (audit V08): the bot prepare deliberately
            # stays on the LEGACY URL-only key — it downloads
            # before the customer picks a quality, and the entry
            # doubles as the job's source via the _preview_quality
            # gate in _tg_submit. Web previews read this entry
            # through make_preview's legacy read path; the
            # tg-file: entries above are keyed the same way.
            pid, pdir = _preview_dir(flow["url"])
            pdir.mkdir(parents=True, exist_ok=True)
            src = pdir / "source.mp4"
            if not _src_valid(src):
                src.unlink(missing_ok=True)
                dl_mod.download(flow["url"], pdir)
        dur = probe_duration(src)
        flow["minutes"] = max(1, math.ceil(dur / 60.0))
        frame = pdir / "tg_frame.jpg"
        sh(["ffmpeg", "-y", "-v", "error", "-ss",
            f"{max(1.0, dur * 0.3):.1f}", "-i", str(src),
            "-frames:v", "1", str(frame)])
        from pipeline import detect_text
        pieces = detect_text.detect_text_pieces(src)
        box = None
        if pieces:
            dom = max(pieces, key=lambda p: p["t1"] - p["t0"])
            box = {k: dom[k] for k in ("x", "y", "w", "h")}
        else:
            box = detect_text.detect_text_box(src)
        flow.update({"pid": pid, "src": str(src), "box": box,
                     "pieces": pieces, "step": "blur"})
        rows = []
        if box:
            shown = pdir / "tg_frame_box.jpg"
            detect_text.draw_box(frame, pieces if pieces else box, shown)
            if len(pieces) >= 2:
                rows.append([("✅ အကုန် ဝါး", "b:ok"),
                             ("🔢 နံပတ် ရွေး ဝါး", "b:pick")])
                rows.append([("မဝါးဘူး", "b:no")])
            else:
                rows.append([("✅ ဒီနေရာ ဝါး", "b:ok"), ("မဝါးဘူး", "b:no")])
            caption = ("🔍 စာတန်း နေရာ တွေ့ပါတယ် — ဘောင် တစ်ခုချင်း ကို "
                       "နံပတ် နဲ့ အရောင် ခွဲထားပါတယ်။ အပိုင်းလိုက် ဝါးပြီး "
                       "မြန်မာ စာတန်း ကို အဓိက နေရာ ထဲ ထည့်ပေးပါမယ်။ "
                       "နေရာ မှန်ပါသလား?")
        else:
            shown = frame
            rows.append([("မဝါးဘူး", "b:no")])
            caption = ("စာတန်း နေရာ auto မတွေ့ပါ — ဝါးချင်တဲ့ အပိုင်း ကို "
                       "ရွေးပေးပါ (မဝါးချင်ရင် «မဝါးဘူး» ကို နှိပ်ပါ)")
        rows.append([("အပေါ်", "b:top"), ("အလယ်", "b:mid"),
                     ("အောက်", "b:bot")])
        flow["blur_rows"] = rows
        tg.send_photo(chat_id, shown, caption=caption, buttons=rows)
    except Exception as e:
        _TG_FLOW.pop(chat_id, None)
        try:
            tg.send_message(chat_id, "🙏 တောင်းပန်ပါတယ် — video ယူလို့ မရပါ ဖြစ်သွားပါတယ်: " + str(e)[:200])
        except Exception:
            pass


def _tg_ask_mode(chat_id):
    from pipeline import telegram_send as tg
    tg.send_message(chat_id, "ဘယ် ပုံစံ နဲ့ ထုတ်ချင်ပါသလဲ?",
                    buttons=[[("နှစ်မျိုးလုံး", "m:both")],
                             [("စာတန်း ပဲ", "m:subs"),
                              ("အသံ ပဲ", "m:dub")]])


def _tg_pick_text(flow):
    pick = sorted(flow.get("pick") or [])
    sel = ", ".join(str(i + 1) for i in pick) if pick else "မရှိသေး"
    return ("🔢 ဝါးမယ့် နံပတ် တွေကို နှိပ်ပြီး ရွေးပေးပါ\n"
            f"ရွေးထားတာ: {sel}")


def _tg_pick_rows(flow):
    pieces = flow.get("pieces") or []
    pick = flow.get("pick") or set()
    rows, row = [], []
    for i in range(len(pieces)):
        row.append((f"{'✅ ' if i in pick else ''}{i + 1}", f"n:{i}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([("✔️ ဒါပဲ ဝါး", "n:go"), ("↩️ နောက်ပြန်", "n:back")])
    return rows


# Build 200: the bot voice step offers the same six voice STYLES
# as the web wizard, from the one shared table in pipeline.tts —
# the callback suffix IS the style key ("v:" + key), stored on the
# flow and resolved to (voice, pitch, rate) at job creation.


_TG_SUB_COLORS = ("white", "yellow", "blue", "green", "red", "black")


# Build 206: while the owner tests the ElevenLabs premium
# voices they are gated (elevenlabs_tts.elevenlabs_allowed):
# test mode = admins + the Admin-card test usernames only. The
# bot judges a chat by its LINKED account; the fixed Telegram
# admin chat counts as an admin principal even when unlinked
# (its jobs already bypass the account checks below).
_EL_GATE_MSG = ("🙏 တောင်းပန်ပါတယ် — ဒီ 🌟 Premium အသံ က အခု "
                "စမ်းသပ်ဆဲ ဖြစ်လို့ သတ်မှတ်ထားတဲ့ account တွေ ပဲ "
                "သုံးလို့ရပါသေးတယ်။ မကြာခင် အားလုံး အတွက် "
                "ဖွင့်ပေးပါမယ်")


def _tg_el_subject(chat_id):
    from pipeline import telegram_send as tg
    acct = accounts.user_for_tg(chat_id)
    if acct is None and tg.is_admin(chat_id):
        return {"role": "admin", "username": ""}
    return acct


def _tg_voice_step(chat_id, flow):
    from pipeline import telegram_send as tg
    flow["step"] = "voice"
    styles = tts.VOICE_STYLES
    rows = [[("🎙 " + s["label"], "v:" + s["key"])
             for s in styles[i:i + 2]]
            for i in range(0, len(styles), 2)]
    # Build 205: ElevenLabs premium voices join the step ONLY
    # when the owner configured a key + registry (picker_entries
    # is empty otherwise). Their callback is "e:<voice_id>" —
    # distinct from the "v:<style>" buttons — and the stored pick
    # is the same "el:<voice_id>" value the web wizard sends.
    # Build 206: picker_entries also judges the chat's linked
    # account through the access gate — in test mode a customer
    # chat never sees these buttons at all. Build 207: the
    # button shows "Name · Tag" when the voice carries a tag.
    for e in elevenlabs_tts.picker_entries(_tg_el_subject(chat_id)):
        label = "🌟 " + e["label"]
        if e.get("tag"):
            label += " · " + e["tag"]
        rows.append([(label + f" ({e['rate']} credits/မိနစ်)",
                      "e:" + elevenlabs_tts.voice_id_of(e["id"]))])
    # Build 212: the chat's OWN clones join the step too (the
    # linked account's rows from the clone store — ownership is
    # the permission, so they show even in test mode; the
    # callback is the same "e:<voice_id>" as a registry pick).
    acct = accounts.user_for_tg(chat_id)
    if acct and elevenlabs_tts.key_configured():
        rate = elevenlabs_tts.credits_per_min()
        for c in accounts.clones_for(acct["id"]):
            rows.append([(f"🎙 {c['name']} ({rate} credits/မိနစ်)",
                          "e:" + c["voice_id"])])
    # Build 229: the Gemini voices join the step too (empty
    # unless a Gemini key is configured), two per row like the
    # built-ins; the callback "gm:<Name>" stores the same
    # "gm:<Name>" pick the web wizard sends. The rate suffix
    # only shows when the owner raised the Gemini rate above
    # the default 1.
    gm_entries = gemini_tts.picker_entries()
    gm_rate = gemini_tts.credits_per_min()
    for i in range(0, len(gm_entries), 2):
        row = []
        for e in gm_entries[i:i + 2]:
            label = "✨ " + e["label"]
            if e.get("tag"):
                label += " · " + e["tag"]
            if gm_rate != _standard_credits_per_min():
                label += f" ({gm_rate} credits/မိနစ်)"
            row.append((label, "gm:" + e["name"]))
        rows.append(row)
    tg.send_message(chat_id, "အသံ ရွေးပေးပါ", buttons=rows)


def _tg_speed_step(chat_id, flow):
    from pipeline import telegram_send as tg
    flow["step"] = "speed"
    tg.send_message(chat_id, "အသံ အရှိန် ရွေးပေးပါ",
                    buttons=[[("1.0×", "s:1.0"),
                              ("1.3× ပုံမှန်", "s:1.3"),
                              ("1.5×", "s:1.5")],
                             [("2×", "s:2.0"), ("3×", "s:3.0"),
                              ("4×", "s:4.0")]])


def _tg_quality_step(chat_id, flow):
    """Download-quality step (build 167), right after the frame
    step: «အကြည် HD» fetches the original rendition (the owner's
    standing wish); 720p/480p fetch a lighter file."""
    from pipeline import telegram_send as tg
    flow["step"] = "quality"
    tg.send_message(chat_id, "Video အရည် ရွေးပေးပါ",
                    buttons=[[("📺 အကြည် HD", "q:hd"),
                              ("720p", "q:720"),
                              ("480p", "q:480")]])


def _tg_logo_step(chat_id, flow):
    from pipeline import telegram_send as tg
    flow["step"] = "logo"
    tg.send_message(chat_id, "Logo ထည့်ဦးမလား",
                    buttons=[[("🚫 မထည့်", "l:skip"),
                              ("🖼 ထည့်မယ်", "l:yes")]])


def _tg_after_logo(chat_id, flow):
    """Logo settled (placed or skipped): modes that burn subtitles
    (both/subs) pick a subtitle colour next; dub-only has no
    subtitles, so it goes straight to the voice step."""
    from pipeline import telegram_send as tg
    if (flow.get("mode") or "both") == "dub":
        _tg_voice_step(chat_id, flow)
        return
    flow["step"] = "color"
    tg.send_message(chat_id, "စာတန်း အရောင် ရွေးပေးပါ",
                    buttons=[[("🤍 အဖြူ", "c:white"),
                              ("💛 အဝါ", "c:yellow"),
                              ("💙 အပြာ", "c:blue")],
                             [("💚 အစိမ်း", "c:green"),
                              ("❤️ အနီ", "c:red"),
                              ("🖤 အမဲ", "c:black")]])


def _tg_karaoke_step(chat_id, flow):
    """Karaoke yes/no step (build 231), right after the
    subtitle-colour step — only flows that burn subtitles
    (both / subs) ever reach it; dub-only skips colour AND
    karaoke, exactly like the web wizard's subtitle step."""
    from pipeline import telegram_send as tg
    flow["step"] = "karaoke"
    tg.send_message(
        chat_id, "🎤 Karaoke စာတန်း ထည့်မလား "
                 "(ဈေးနှုန်း မပြောင်းပါ)",
        buttons=[[("🎤 ထည့်မယ်", "k:1"),
                  ("မထည့်ဘူး", "k:0")]])


def _tg_start_flow_job(chat_id, flow):
    """Final bot-flow step: credit/cap checks, then create the job
    from everything the flow collected (blur, mode, frame, quality,
    logo, colour, voice, speed)."""
    from pipeline import telegram_send as tg
    ratio = flow.get("ratio") or "orig"
    acct = accounts.user_for_tg(chat_id)
    if elevenlabs_tts.is_el_voice(flow.get("voice_style")):
        # Build 212: the account's OWN clone is exempt from the
        # gate (ownership is the permission, as on the web
        # surfaces); every other premium pick keeps the rule.
        _vid = elevenlabs_tts.voice_id_of(flow["voice_style"])
        _clone = accounts.clone_by_voice(_vid) if acct else None
        _own_clone = bool(_clone and acct
                          and _clone["user_id"] == acct["id"])
        if not _own_clone \
                and not elevenlabs_tts.elevenlabs_allowed(
                    _tg_el_subject(chat_id)):
            # Build 206: the credit pre-check below must agree
            # with the gate — a premium pick this chat may not
            # use (mode flipped mid-flow, crafted callback) is
            # refused HERE, before any quote math, and no job
            # is created.
            _TG_FLOW.pop(chat_id, None)
            tg.send_message(chat_id, _EL_GATE_MSG)
            return
    if not tg.is_admin(chat_id):
        if not acct:
            _TG_FLOW.pop(chat_id, None)
            tg.send_message(chat_id, "🙏 video ထုတ်ဖို့ အရင် 👤 Username ချိတ်ပေးပါ "
                                     "(အောက်က Menu ထဲက) — account "
                                     "ချိတ်ပြီးမှ ထုတ်လို့ရပါမယ်" + _LINK_HINT)
            return
        # Build 187: one-time setup gate — linking a chat is fine
        # before setup, but a Google-born account that never
        # finished setup on the web cannot start bot jobs either
        # (the same central job-start rule as the web route).
        setup_msg = _setup_gate_message(acct)
        if setup_msg:
            _TG_FLOW.pop(chat_id, None)
            tg.send_message(chat_id, setup_msg +
                            " web page ထဲမှာ ဝင်ပြီး ပြင်ပေးပါ 🙏")
            return
        need = flow.get("minutes") or 1
        if elevenlabs_tts.is_el_voice(flow.get("voice_style")):
            # Build 205: ElevenLabs premium voices bill at the
            # admin-set per-minute rate — the same quote math as
            # the web surface.
            need *= elevenlabs_tts.credits_per_min()
        if gemini_tts.is_gm_voice(flow.get("voice_style")):
            # Build 229: a Gemini pick bills the Gemini rate
            # (default 1 — a no-op until the owner raises it).
            need *= gemini_tts.credits_per_min()
        if (acct["credits"] or 0) < need:
            _TG_FLOW.pop(chat_id, None)
            tg.send_message(chat_id,
                            f"💳 တောင်းပန်ပါတယ် — credit မလောက်ပါ။ ဒီ video အတွက် {need} "
                            f"credits လိုပါတယ်, လက်ကျန် "
                            f"{acct['credits']} credits ပဲ ရှိပါတယ်။ Menu ထဲက «💰 Credit ဖြည့်» နဲ့ ဖြည့်နိုင်ပါတယ် 🙏")
            return
        # Daily limit (build 180): the ACCOUNT's day total counts
        # web + Telegram jobs together; refused before the job
        # exists, so nothing is ever charged for it.
        limit_msg = _daily_limit_message(acct)
        if limit_msg:
            _TG_FLOW.pop(chat_id, None)
            tg.send_message(chat_id, limit_msg)
            return
    if (not tg.is_admin(chat_id)
            and _tg_daily_count(chat_id) >= _TG_DAILY_CAP):
        _TG_FLOW.pop(chat_id, None)
        tg.send_message(chat_id, "🚫 တောင်းပန်ပါတယ် — ဒီနေ့ အတွက် job ကန့်သတ် ပြည့်သွားပါပြီ။ "
                                 "မနက်ဖြန် ပြန် ကြိုးစားပေးပါ 🙏")
        return
    _TG_FLOW.pop(chat_id, None)
    job, err = _new_job({"url": flow.get("url")
                         or "https://telegram.org/file",
                         "mode": flow.get("mode") or "both",
                         "voice_style": flow.get("voice_style"),
                         "karaoke": bool(flow.get("karaoke")),
                         "subs": ({"color": flow["sub_color"]}
                                  if flow.get("sub_color") else None),
                         "speed_cap": flow.get("speed_cap") or 1.3,
                         "quality": flow.get("quality") or "hd"},
                        user=_tg_el_subject(chat_id))
    if err:
        tg.send_message(chat_id, "🙏 တောင်းပန်ပါတယ် — " + err)
        return
    if flow.get("file_id"):
        job["url"] = "📁 Telegram file"
    if acct and not tg.is_admin(chat_id):
        job["user_id"] = acct["id"]
        accounts.mark_has_job(acct["id"])
    if flow.get("file_id"):
        # An uploaded file IS the source — no download quality
        # applies, always reuse it.
        job["source_path"] = flow.get("src")
    elif flow.get("src") and _src_valid(Path(flow["src"])) \
            and _preview_quality(
                Path(flow["src"]).parent) == job["quality"]:
        # Build 167: the prepare download doubles as the job's
        # source only when it was fetched at the picked quality;
        # otherwise phase 1 downloads fresh at that quality.
        # Build 183: _src_valid (not just the flow's say-so) — the
        # cached file may have been cleaned up or truncated since
        # prepare; a bad cache falls back to a fresh download.
        job["source_path"] = flow.get("src")
    fx = {}
    if flow.get("blur"):
        fx["blur"] = {**flow["blur"], "strength": 25}
    if flow.get("blur_pieces"):
        fx["blur_pieces"] = flow["blur_pieces"]
    if flow.get("logo"):
        fx["logo"] = flow["logo"]
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
    _enqueue_job(job)
    ahead = _active_worker_count() + len(_JOB_QUEUE) - 1
    if ahead >= MAX_CONCURRENT_JOBS:
        tg.send_message(chat_id,
                        f"✅ လက်ခံရရှိပါပြီ — တန်းစီ {ahead - MAX_CONCURRENT_JOBS + 1} ခုမြောက် ဖြစ်ပါတယ်။ "
                        "အရင် job တွေ ပြီးတာနဲ့ စလုပ်ပေးပါမယ် 🙏")
    else:
        tg.send_message(chat_id,
                        "✅ လက်ခံရရှိပါပြီ — စလုပ်နေပါပြီ။ ပြီးတာနဲ့ ဒီမှာပဲ "
                        "ဗီဒီယို တန်း ပို့ပေးပါမယ် 🙏")


def _on_tg_callback(cq):
    """Inline-button taps for the bot flow: blur, mode, frame,
    quality, logo, colour, voice, speed."""
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
                        f"💳 Credit {res['credits']} ဝင်ပါပြီ — "
                        "ကျေးဇူးတင်ပါတယ် 🙏" if act == "ok" else
                        "🙏 တောင်းပန်ပါတယ် — Credit ဖြည့် တောင်းဆိုချက် ကို လက်ခံလို့ မရပါ ဖြစ်သွားပါတယ်။ လွှဲပြီး screenshot ကို စစ်ပြီး ပြန် ပို့ကြည့်ပေးပါ")
                except Exception:
                    pass
            tg.send_message(chat_id, f"ဆုံးဖြတ်ပြီး (#{tid})")
        elif act == "ok":
            tg.send_message(chat_id, "⚠️ approve မရပါ — ဆုံးဖြတ်ပြီးသား "
                                     "ဖြစ်နိုင်သလို screenshot မရောက်သေးတာ "
                                     "လည်း ဖြစ်နိုင်ပါတယ် (screenshot မရှိရင် "
                                     "approve ပိတ်ထားပါတယ်)")
        return
    flow = _TG_FLOW.get(chat_id)
    if not flow:
        return
    if time.time() - flow.get("ts", 0) > 30 * 60:
        _TG_FLOW.pop(chat_id, None)
        tg.send_message(chat_id, "⏰ အချိန် ကုန်သွားပါပြီ — link ကို ပြန်ပို့ပေးပါ 🙏")
        return
    if data.startswith("p:") and flow.get("step") == \
            "topup_amount":
        # Build 231: a package tap. The amount is the package's
        # TOTAL credits (base + bonus); the kyat price rides
        # along so approval credits it as one package entry.
        try:
            kyat = int(data[2:])
        except ValueError:
            return
        pkg = PACKAGES.get(kyat)
        if not pkg:
            return
        flow["amount"] = pkg[0]
        flow["package"] = kyat
        flow["step"] = "topup_proof"
        tg.send_message(
            chat_id,
            f"📦 Package {kyat:,} ကျပ် → {pkg[0]:,} credits"
            + (f" (bonus +{pkg[1]:,})" if pkg[1] else "")
            + "\n📸 ငွေ လွှဲပြီးရင် screenshot ပို့ပေးပါ 🙏")
        return
    if data.startswith("b:") and flow.get("step") == "blur":
        choice = data[2:]
        if choice == "pick":
            flow["step"] = "blurpick"
            flow["pick"] = set()
            res = tg.send_message(chat_id, _tg_pick_text(flow),
                                  buttons=_tg_pick_rows(flow))
            if isinstance(res, dict):
                flow["pick_msg"] = res.get("message_id")
            return
        if choice == "ok":
            flow["blur"] = flow.get("box")
            flow["blur_pieces"] = flow.get("pieces")
        elif choice == "no":
            flow["blur"] = None
            flow["blur_pieces"] = None
        else:
            flow["blur"] = _BLUR_PRESETS.get(choice)
            flow["blur_pieces"] = None
        flow["step"] = "mode"
        _tg_ask_mode(chat_id)
        return
    if data.startswith("n:") and flow.get("step") == "blurpick":
        act = data[2:]
        if act == "back":
            flow["step"] = "blur"
            tg.send_message(chat_id, "ဝါးမယ့် နေရာ ပြန် ရွေးပေးပါ 🙏",
                            buttons=flow.get("blur_rows") or
                            [[("✅ အကုန် ဝါး", "b:ok"),
                              ("မဝါးဘူး", "b:no")]])
            return
        if act == "go":
            pick = flow.get("pick") or set()
            pieces = flow.get("pieces") or []
            chosen = [pieces[i] for i in sorted(pick) if i < len(pieces)]
            if not chosen:
                tg.send_message(chat_id, "အရင် နံပတ် တစ်ခု ရွေးပေးပါ 🙏")
                return
            flow["blur_pieces"] = chosen
            dom = max(chosen, key=lambda p: p["t1"] - p["t0"])
            flow["blur"] = {k: dom[k] for k in ("x", "y", "w", "h")}
            flow["step"] = "mode"
            _tg_ask_mode(chat_id)
            return
        try:
            idx = int(act)
        except ValueError:
            return
        pick = flow.setdefault("pick", set())
        if idx in pick:
            pick.discard(idx)
        else:
            pick.add(idx)
        try:
            tg.edit_message(chat_id, flow.get("pick_msg"),
                            _tg_pick_text(flow),
                            buttons=_tg_pick_rows(flow))
        except Exception:
            res = tg.send_message(chat_id, _tg_pick_text(flow),
                                  buttons=_tg_pick_rows(flow))
            if isinstance(res, dict):
                flow["pick_msg"] = res.get("message_id")
        return
    if data.startswith("m:") and flow.get("step") == "mode":
        mode = data[2:]
        if mode not in ("both", "subs", "dub"):
            return
        flow["mode"] = mode
        flow["step"] = "frame"
        tg.send_message(chat_id, "Frame (ဗီဒီယို အချိုး) ရွေးပေးပါ",
                        buttons=[[("မူရင်း", "f:orig"), ("9:16", "f:9:16"),
                                  ("16:9", "f:16:9")],
                                 [("1:1", "f:1:1"), ("4:5", "f:4:5")]])
        return
    if data.startswith("f:") and flow.get("step") == "frame":
        ratio = data[2:]
        if ratio != "orig" and ratio not in fx_mod.ASPECT_DIMS:
            return
        flow["ratio"] = ratio
        _tg_quality_step(chat_id, flow)
        return
    if data.startswith("q:") and flow.get("step") == "quality":
        q = data[2:]
        if q not in ("hd", "720", "480"):
            return
        flow["quality"] = q
        _tg_logo_step(chat_id, flow)
        return
    if data.startswith("l:") and flow.get("step") == "logo":
        if data == "l:yes":
            flow["step"] = "logo_img"
            tg.send_message(chat_id, "Logo ပုံ ပို့ပေးပါ (PNG/JPG) — "
                                     "နောက်ခံ မပါတဲ့ PNG ဆို ပိုကောင်းပါတယ် 🙏")
            return
        _tg_after_logo(chat_id, flow)
        return
    if data.startswith("g:") and flow.get("step") == "logo_pos":
        pos = {"g:tl": (0.0, 0.0), "g:tr": (1.0, 0.0),
               "g:bl": (0.0, 1.0), "g:br": (1.0, 1.0)}.get(data)
        if pos and flow.get("logo_file"):
            flow["logo"] = {"path": flow["logo_file"], "pos_x": pos[0],
                            "pos_y": pos[1], "size_pct": 18,
                            "opacity": 70, "saturation": 100}
        _tg_after_logo(chat_id, flow)
        return
    if data.startswith("c:") and flow.get("step") == "color":
        if data[2:] in _TG_SUB_COLORS:
            flow["sub_color"] = data[2:]
        _tg_karaoke_step(chat_id, flow)
        return
    if data.startswith("k:") and flow.get("step") == "karaoke":
        # Build 231: the karaoke tick, same place in the flow
        # as the web wizard's subtitle step.
        flow["karaoke"] = data == "k:1"
        if (flow.get("mode") or "both") == "both":
            _tg_voice_step(chat_id, flow)
            return
        _tg_start_flow_job(chat_id, flow)
        return
    if data.startswith("v:") and flow.get("step") == "voice":
        style = tts.STYLE_BY_KEY.get(data[2:])
        if not style:
            return
        flow["voice_style"] = style["key"]
        _tg_speed_step(chat_id, flow)
        return
    if data.startswith("e:") and flow.get("step") == "voice":
        # Build 205: an ElevenLabs premium tap. The voice must
        # still be in the owner's registry (it may have been
        # removed after these buttons were sent) — a stale tap is
        # ignored, never substituted with a free voice.
        entry = elevenlabs_tts.find_voice(data[2:])
        if not entry:
            # Build 212: not a registry voice — it may be the
            # chat's OWN clone (same ownership rule as the web
            # surfaces). Someone else's clone is a stale tap:
            # ignored, exactly like a removed registry voice.
            acct = accounts.user_for_tg(chat_id)
            clone = accounts.clone_by_voice(data[2:]) if acct else None
            if not clone or clone["user_id"] != acct["id"]:
                return
            flow["voice_style"] = "el:" + clone["voice_id"]
            _tg_speed_step(chat_id, flow)
            return
        # Build 206: the access gate judges the tap too — the
        # buttons may predate a mode flip, or the callback may
        # be replayed by a chat that never saw them. Refuse
        # with a polite note; the flow stays on the voice step.
        if not elevenlabs_tts.elevenlabs_allowed(
                _tg_el_subject(chat_id)):
            tg.send_message(chat_id, _EL_GATE_MSG)
            return
        flow["voice_style"] = "el:" + entry["id"]
        _tg_speed_step(chat_id, flow)
        return
    if data.startswith("gm:") and flow.get("step") == "voice":
        # Build 229: a Gemini tap. The voice must still be in
        # the 30-voice table — a stale tap is ignored, never
        # substituted with another voice (the EL rule).
        entry = gemini_tts.find_voice(data[3:])
        if not entry:
            return
        flow["voice_style"] = "gm:" + entry["name"]
        _tg_speed_step(chat_id, flow)
        return
    if data.startswith("s:") and flow.get("step") == "speed":
        try:
            speed = float(data[2:])
        except ValueError:
            return
        if speed not in (1.0, 1.3, 1.5, 2.0, 3.0, 4.0):
            return
        flow["speed_cap"] = speed
        _tg_start_flow_job(chat_id, flow)
        return


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


def _job_dispatcher():
    """Feed the worker slots (build 180: MAX_CONCURRENT_JOBS = 4
    — the owner wants the full 4-core YTA 3 working).

    Every submission — web form, Telegram bot, review re-render —
    lands in _JOB_QUEUE with status "queued" and starts here, in
    order, as slots free up. Nothing is ever refused for
    concurrency; it waits. A job cancelled while waiting is settled
    as cancelled without burning a worker slot."""
    while True:
        try:
            while _JOB_QUEUE and \
                    _active_worker_count() < MAX_CONCURRENT_JOBS:
                jid, kind = _JOB_QUEUE.pop(0)
                job = JOBS.get(jid)
                if not job or job.get("status") != "queued":
                    continue
                if job.get("cancel_requested"):
                    _cancelled(job)
                    continue
                job["worker_started"] = True
                # Build 186 (audit fix #7): record the queue wait
                # in the job's timings — phase_s used to start at
                # the download step, so a job that waited minutes
                # for a slot looked "fast" in every report.
                try:
                    waited = round(
                        time.time() - float(job.get("enqueued_at")
                                            or job.get("created_at")
                                            or time.time()), 1)
                    job["queue_wait_s"] = waited
                    job.setdefault("phase_s", {})["queue_wait"] = waited
                    # Build 213: the same wait in the canonical
                    # timings dict (queue_s).
                    job.setdefault("timings", {})["queue_s"] = waited
                except Exception:
                    pass
                if jid in _TG_PENDING:
                    _TG_PENDING.remove(jid)
                target = {"rerun": _worker2,
                          "recap1": _recap_worker,
                          "recap2": _recap_worker2,
                          "story1": _story_worker,
                          "story2": _story_worker2,
                          "commentary1": _commentary_worker,
                          "commentary2": _commentary_worker2,
                          "captions1": _captions_worker,
                          "captions2": _captions_worker2,
                          "captions_api": _api_captions_worker,
                          "download1": _download_worker}.get(
                              kind, _worker)
                wt = threading.Thread(target=target, args=(job,),
                                      daemon=True)
                # Build 204 (audit V09): registered BEFORE start
                # so a cancel landing in this window still finds
                # the thread and waits for it (see _stop_and_cleanup).
                _WORKER_THREADS[jid] = wt
                wt.start()
                if job.get("tg_chat_id"):
                    threading.Thread(target=_tg_progress, args=(job,),
                                     daemon=True).start()
        except Exception:
            pass
        time.sleep(2)


# Worker threads by job id (build 204, audit V09): the cancel
# path waits for the job's worker to actually exit before its
# files are removed. Entries are set by the dispatcher just
# before the thread starts and dropped here in _cleanup_job.
_WORKER_THREADS = {}


def _cleanup_job(jid):
    # Build 204 (audit V09/V10): tombstone FIRST — from this
    # moment every late _save for this job is a no-op, so a
    # still-unwinding worker cannot resurrect job.json behind
    # the rmtree below — then drain the per-job save lock so an
    # in-flight save finishes (into the still-existing dir)
    # before any file is removed.
    _tombstone(jid)
    _lk = _save_lock(jid)
    with _lk:
        pass
    with _SAVE_LOCKS_GUARD:
        if _SAVE_LOCKS.get(jid) is _lk:
            _SAVE_LOCKS.pop(jid, None)
    _WORKER_THREADS.pop(jid, None)
    CLEANUP_TIMERS.pop(jid, None)
    job = JOBS.pop(jid, None)
    url = (job or {}).get("url")
    source_path = (job or {}).get("source_path")
    if not url:
        try:
            meta = json.loads((JOBS_DIR / jid / "job.json").read_text(encoding="utf-8"))
            url = meta.get("url")
            source_path = source_path or meta.get("source_path")
        except Exception:
            url = None
    shutil.rmtree(JOBS_DIR / jid, ignore_errors=True)
    # Build 203 (audit V08): drop EVERY preview entry this job's
    # URL owns — the legacy dir plus each quality-keyed dir —
    # and the dir this job's own source_path pointed into (the
    # bot's tg-file: entries live under neither URL key). Any
    # dir an ACTIVE job still references via its source_path is
    # skipped: one job's cleanup must not pull the source out
    # from under a queued/running sibling on the same link.
    keep = _active_preview_dirs()
    targets = []
    if url:
        targets += [pdir for _pid, pdir in _preview_dirs_for(url)]
    if source_path:
        parent = Path(source_path).parent
        if parent.parent == PREVIEWS_DIR:
            targets.append(parent)
    for pdir in targets:
        if str(pdir) not in keep:
            shutil.rmtree(pdir, ignore_errors=True)


def _job_minutes(job):
    src = JOBS_DIR / job["id"] / "source.mp4"
    try:
        return max(1, math.ceil(probe_duration(src) / 60.0))
    except Exception:
        return 1


def _credits_per_min_for(job):
    """Per-minute credit rate for a job (build 205): an
    ElevenLabs premium voice bills at the admin-set
    ELEVENLABS_CREDITS_PER_MIN (default 2); every other voice —
    and subtitles-only jobs, whose voice is never premium — is
    the standard 1 credit per started minute."""
    if job.get("type") == "commentary":
        # Build 217: Commentary bills its own per-minute rates.
        # Build 231: explicit per-tier rates from the approved
        # card (std 80 / Gemini 120 / ElevenLabs 160) — build
        # 229's std x Gemini-rate formula cannot express the
        # card now that every tier is an absolute new-unit rate.
        if elevenlabs_tts.is_el_voice(job.get("voice")):
            return _commentary_elevenlabs_credits_per_min()
        if gemini_tts.is_gm_voice(job.get("voice")):
            return _commentary_gemini_credits_per_min()
        return _commentary_credits_per_min()
    if elevenlabs_tts.is_el_voice(job.get("voice")):
        return elevenlabs_tts.credits_per_min()
    # Build 229: Gemini voices bill the GEMINI_CREDITS_PER_MIN
    # setting (default 80 since build 231) — read live at
    # charge time, so the owner's price change needs no rebuild.
    if gemini_tts.is_gm_voice(job.get("voice")):
        return gemini_tts.credits_per_min()
    return _standard_credits_per_min()


def _credits_for_minutes(job, minutes):
    """Total credits for a job of `minutes` started minutes —
    THE figure the admission quote, the upload balance
    pre-check and the success charge all use, so they can
    never disagree. Every product bills minutes x its
    per-minute rate except Captions-only (build 219), whose
    rate is fractional (half the dub rate by default):
    ceil(minutes x rate), minimum 1 credit."""
    if job.get("type") == "download":
        # Build 234: the download-only product bills started
        # minutes x its own flat per-minute rate (default 10
        # credits = 50 kyat/min, owner-set 2026-11-20). No
        # minimum: a sub-minute clip is exactly 1 minute.
        return minutes * _download_credits_per_min()
    if job.get("type") == "captions":
        # Build 231: the minimum charge is 30 credits
        # (= 150 kyat — the same kyat value as the OLD
        # code's 1-credit minimum: max(1, ceil(min x 0.5))
        # could never charge less than 1 old credit). It
        # binds below 1.5 started minutes; from 2 minutes
        # the 20/min rate card takes over exactly.
        return max(30, math.ceil(
            minutes * _captions_credits_per_min()))
    return minutes * _credits_per_min_for(job)


def _job_is_admin(job):
    """Build 232: True when the job belongs to an ADMIN account
    — the exact lookup the charge path uses to exempt a job
    (_quote_for_job / _charge_for_job below): the job's user_id
    resolved through accounts, role == "admin". Anonymous jobs
    (no user_id) are never charged either, but they are NOT
    admin and return False here. The flag only steers the
    translation walk order for the main video job path (admin
    jobs lead with Gemini, then OpenAI — see
    pipeline.translate_segments)."""
    uid = job.get("user_id")
    if not uid:
        return False
    user = accounts.get_user(uid)
    return bool(user) and user["role"] == "admin"


def _quote_for_job(job, minutes=None):
    """Admission check after phase 1 (the duration is known): a
    non-admin account must COVER the job's credits — started
    minutes × its per-minute rate (1 standard; the admin-set
    premium rate for ElevenLabs voices, build 205) — but nothing
    is deducted here. Owner rule (2026-11-09): credits move only
    when the video is actually produced; a job that fails or is
    cancelled is never charged. `minutes` (build 234) lets a
    caller that measured the duration itself (the download-only
    worker, from source_info.json / ffprobe) quote on ITS
    figure; every existing caller leaves it None and keeps the
    _job_minutes probe of source.mp4."""
    uid = job.get("user_id")
    if not uid:
        return
    user = accounts.get_user(uid)
    if not user or user["role"] == "admin":
        return
    if minutes is None:
        minutes = _job_minutes(job)
    job["quote_minutes"] = minutes
    # Build 205: the quote is in CREDITS — minutes × the job's
    # per-minute rate (premium ElevenLabs voices cost more than
    # the standard 1/minute). quote_minutes stays on the record
    # as the duration figure; quote_credits is what moves.
    needed = _credits_for_minutes(job, minutes)
    job["quote_credits"] = needed
    _save(job)
    if (user["credits"] or 0) < needed:
        raise RuntimeError(
            f"credit မလောက်ပါ — ဒီ video အတွက် {needed} credits "
            f"လိုပါတယ်, လက်ကျန် {user['credits']} credits ပဲ ရှိပါတယ်")


def _charge_for_job(job):
    """Success-only charge, called from _finish once the video
    exists: deduct the quoted credits. If the balance dropped below
    the quote after admission (a concurrent job spent it), take what
    is there and record the shortfall — the finished video is still
    delivered. Admin and anonymous jobs are never charged."""
    uid = job.get("user_id")
    if not uid or job.get("charged_minutes"):
        return
    user = accounts.get_user(uid)
    if not user or user["role"] == "admin":
        return
    # Build 205: charge the quoted CREDITS (minutes × rate).
    # quote_credits is set by _quote_for_job on every fresh run;
    # the fallbacks keep pre-205 records (minutes only) exact.
    minutes = job.get("quote_credits") or job.get("quote_minutes") \
        or _credits_for_minutes(job, _job_minutes(job))
    paid = minutes
    # Build 234: the ledger kind distinguishes the product —
    # download-only jobs record "download", everything else
    # keeps the historical "job".
    reason = "download" if job.get("type") == "download" else "job"
    if not accounts.charge(uid, minutes, job["id"], reason=reason):
        paid = max(0, min(user["credits"] or 0, minutes))
        if paid:
            accounts.charge(uid, paid, job["id"], reason=reason)
        if paid < minutes:
            job["charge_shortfall"] = minutes - paid
    job["charged_minutes"] = paid
    _save(job)
    _maybe_referral_reward(job)  # build 221


def _refund_charge(job):
    # Charges now settle only at _finish (charge_settled), after
    # which a failure can no longer occur — so this is a no-op safety
    # net for any pre-finish charge, kept for symmetry.
    if job.get("charge_settled"):
        return
    uid = job.get("user_id")
    minutes = job.get("charged_minutes") or 0
    if uid and minutes:
        accounts.refund(uid, minutes, job["id"])
        job["charged_minutes"] = 0


def _fail(job, e):
    _timing_close(job)  # build 213: settle the timing span first
    _refund_charge(job)
    job["status"] = "error"
    if job.get("current") == "download":
        # Build 166 (fix F1): download failures reach the customer
        # (web card + Telegram) as a polite Burmese category; the
        # raw error stays in the server log. Other steps keep their
        # own composed messages (translation failures already carry
        # a polite Burmese explanation + provider breakdown).
        print(f"[job {job['id']}] download FAILED raw: "
              f"{str(e)[:400]}", flush=True)
        job["error"] = dl_mod.polite_error(e, job.get("url") or "")
    else:
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
                       max_speed=job.get("speed_cap") or 1.3,
                       quality=job.get("quality") or "hd",
                       voice_name=job.get("voice_name"),
                       karaoke=bool(job.get("karaoke")))


def _finish(job):
    _timing_close(job)  # build 213: settle the timing span first
    # Build 204 (audit V09): a cancel that landed after the last
    # progress boundary (i.e. during the final phase, with no
    # further progress call to raise JobCancelled) must not flip
    # the job to done — and must not charge it. Settle as
    # cancelled instead; _cancelled refunds/no-ops the charge
    # exactly as for a boundary cancel.
    if job.get("cancel_requested"):
        _cancelled(job)
        return
    # Build 186 (audit fix #1) moved the small copy OFF this path
    # into a background thread; build 198 removes the finish-time
    # copy entirely — it is generated lazily on the first ?v=small
    # request (_small_on_demand), so a job nobody downloads costs
    # no second encode at all. _finish now only settles the charge
    # and flips the job to done. Any small copy left from a
    # previous render of this job is stale for the new output, so
    # its metadata (and any lazy-generation verdict) is dropped
    # here, synchronously; the stale FILE is replaced by the next
    # on-demand generation.
    job.pop("small", None)
    job.pop("small_na", None)
    _out = JOBS_DIR / job["id"] / "output.mp4"
    try:
        if _out.exists():
            job["output_size"] = _out.stat().st_size
    except Exception:
        pass
    job["small_pending"] = False
    _charge_for_job(job)
    job["charge_settled"] = True
    job["status"] = "done"
    job["current"] = None
    job["finished_at"] = time.time()
    _schedule_cleanup(job)
    _save(job)
    _tg_deliver_async(job)
    _web_tg_autosend(job)


def _cancelled(job):
    _timing_close(job)  # build 213: settle the timing span first
    _refund_charge(job)
    job["status"] = "cancelled"
    job["error"] = "ရပ်လိုက်ပြီ — ဖျက်ရန် တောင်းဆိုထားသည်"
    job["finished_at"] = time.time()
    # Build 215: cancelled is a finished state like any other —
    # the same single 24h rule applies (the pre-215 code deleted
    # cancelled jobs after only 20 seconds).
    _schedule_cleanup(job)
    _save(job)


def _worker(job):
    """Full run: download -> transcribe -> translate -> TTS/retime/burn.

    No mandatory review stop: the auto-translated Myanmar text is used
    as-is and the video renders straight through. The user can still
    edit the text afterwards and re-render (see job_review)."""
    workdir = JOBS_DIR / job["id"]
    threading.Thread(target=_heartbeat, args=(job,), daemon=True).start()
    # Build 204 (audit V09): the whole run executes under this
    # job's process context — every subprocess the pipeline spawns
    # from this thread is registered to the job, so a cancel can
    # terminate the actual work (see pipeline.common.run).
    with job_context(job["id"]):
        try:
            job["status"] = "running"
            _timing_start(job)  # build 213
            _save(job)
            run_job.run_phase1(workdir, job["url"], source_path=job.get("source_path"),
                               progress=_progress_fn(job),
                               style="subs" if job["mode"] == "subs" else "dub",
                               quality=job.get("quality") or "hd",
                               force_source=bool(job.get("upload")),
                               admin=_job_is_admin(job))
            if job.get("upload"):
                # Build 215: an uploaded source has no download
                # stage at all — the timing says so exactly.
                job.setdefault("timings", {})["download"] = 0
            _quote_for_job(job)
            _run_phase2(job)
            _finish(job)
        except JobCancelled:
            _cancelled(job)
        except Exception as e:
            # A failure CAUSED by the cancel path (its ffmpeg was
            # terminated mid-flight) is a cancellation, not an
            # error — never flip a cancelled job to "error".
            if job.get("cancel_requested"):
                _cancelled(job)
            else:
                _fail(job, e)  # surface the real error to the UI


def _worker2(job):
    """Re-render phase 2 from the current (possibly edited) segments."""
    threading.Thread(target=_heartbeat, args=(job,), daemon=True).start()
    with job_context(job["id"]):
        try:
            job["status"] = "running"
            _timing_start(job)  # build 213
            _save(job)
            _run_phase2(job)
            _finish(job)
        except JobCancelled:
            _cancelled(job)
        except Exception as e:
            if job.get("cancel_requested"):
                _cancelled(job)
            else:
                _fail(job, e)


@app.get("/")
def index():
    resp = send_from_directory(ROOT / "static", "index.html")
    # The shell must never come from a browser cache — a stale copy
    # looks exactly like "the update didn't land" (build tag and
    # all), which has burned several debugging rounds.
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/static/<path:filename>")
def static_files(filename):
    # static_folder is disabled above; serve the PWA manifest/icons explicitly.
    return send_from_directory(ROOT / "static", filename)


@app.get("/sw.js")
def service_worker():
    # Served from the root so its scope covers the whole app.
    return send_from_directory(ROOT / "static", "sw.js")


@app.get("/api/version")
def api_version():
    # Build 193: lets an already-open page notice it is running an
    # older build than the server (see the check in index.html).
    resp = jsonify({"build": BUILD_TAG})
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _mine_entries(user):
    """The CALLER's own clones in picker shape (build 212) —
    the "mine" array of /api/voices. Empty for logged-out
    callers, and empty while no ElevenLabs key is configured
    (a clone cannot speak without the key, exactly like the
    registry group above). Rows come from the accounts clone
    store; ownership was recorded at creation, so this is a
    pure read of the caller's own rows."""
    if not user or not user.get("id"):
        return []
    if not elevenlabs_tts.key_configured():
        return []
    rate = elevenlabs_tts.credits_per_min()
    return [{"id": "el:" + c["voice_id"], "label": c["name"],
             "name": c["name"], "tag": "Clone",
             "desc": "ကိုယ်ပိုင် clone အသံ",
             "preview": elevenlabs_tts.preview_url_usr(c["voice_id"]),
             "roles": ["narration"],
             "premium": True, "rate": rate}
            for c in accounts.clones_for(user["id"])]


@app.get("/api/voices")
def voices():
    # Build 200: the picker offers voice STYLES from the one
    # shared table in pipeline.tts (the two my-MM voices × three
    # pitch variants) — ids are style keys, resolved to a real
    # voice + pitch at job creation. Static on purpose: the table
    # IS the catalogue, so the picker works even when the live
    # edge-tts voice list cannot be reached.
    # Build 207: every entry also carries the voice-library
    # fields (name / tag / desc / preview) for the library-sheet
    # picker; the built-in previews live at /voice-previews/
    # std-<key>.mp3 and are generated lazily on first request.
    out = [{"id": s["key"], "label": s["label"], "name": s["name"],
            "tag": s["tag"], "desc": s["desc"],
            "roles": s.get("roles") or ["narration"],
            "preview": f"/voice-previews/std-{s['key']}.mp3"}
           for s in tts.style_library()]
    # Build 205: ElevenLabs premium voices join the picker ONLY
    # when the owner has configured a key + registry
    # (picker_entries() is empty otherwise), so an unconfigured
    # install returns exactly the build-200 payload.
    # Build 206: picker_entries() also applies the access gate
    # to the CALLER — in test mode only admins + test accounts
    # receive the premium entries; everyone else (including
    # logged-out page loads) gets exactly the standard list.
    out += elevenlabs_tts.picker_entries(_current_user())
    # Build 229: Gemini voices join whenever a Gemini key is
    # configured (their picker_entries() is empty otherwise) —
    # standard-rate entries with role tags, in the same list.
    out += gemini_tts.picker_entries()
    # Build 208: the payload is an object. "voices" is the
    # catalogue above (built-ins + gate-filtered registry
    # voices — the picker's Ready-made tab). "mine" is the
    # CALLER's own cloned voices. Build 212: the self-serve
    # Voice Clone store is live, so "mine" now reads the
    # caller's real clone rows (still [] for logged-out
    # callers and while ElevenLabs is unconfigured).
    # Build 229: "favs" is the CALLER's own starred voice ids
    # (per-account — never another user's; [] logged out).
    me = _current_user()
    favs = accounts.fav_voices(me["id"]) if me and me.get("id") \
        else []
    return jsonify({"voices": out,
                    "mine": _mine_entries(_current_user()),
                    "favs": favs,
                    "rates": {
                        "standard": _standard_credits_per_min(),
                        "gemini": gemini_tts.credits_per_min(),
                        "elevenlabs":
                            elevenlabs_tts.credits_per_min(),
                        "commentary":
                            _commentary_credits_per_min(),
                        "commentary_gemini":
                            _commentary_gemini_credits_per_min(),
                        "commentary_elevenlabs":
                            _commentary_elevenlabs_credits_per_min(),
                        "captions": _captions_credits_per_min()}})


@app.post("/api/voices/fav")
def voice_fav():
    """Star/unstar a picker voice for the CALLER (build 229).
    Login required; the row is keyed by the caller's user id,
    so favourites are strictly per-account. The voice id is
    the picker's own vocabulary (style key / "el:<id>" /
    "gm:<Name>") — it is stored as-is and only ever read back
    for the same account."""
    user = _current_user()
    if not user or not user.get("id"):
        return jsonify({"error": "အရင် login ဝင်ပါ"}), 401
    data = request.get_json(force=True, silent=True) or {}
    voice_id = str(data.get("voice_id") or "").strip()
    if not voice_id or len(voice_id) > 80:
        return jsonify({"error": "voice_id မမှန်ပါ"}), 400
    if data.get("on"):
        accounts.add_fav(user["id"], voice_id)
    else:
        accounts.remove_fav(user["id"], voice_id)
    return jsonify({"ok": True,
                    "favs": accounts.fav_voices(user["id"])})


_STD_PREVIEW_LOCK = threading.Lock()


def _ensure_std_preview(style_key):
    """Lazily synthesize a built-in style's preview clip ONCE
    and cache it next to the ElevenLabs previews (build 207).
    Returns the file path, or None — unknown style, or the
    synthesis failed (the picker row then simply has no working
    preview; the front-end hides the button on audio error)."""
    path = elevenlabs_tts.PREVIEW_DIR / f"std-{style_key}.mp3"
    if path.exists():
        return path
    if style_key not in tts.STYLE_BY_KEY:
        return None
    with _STD_PREVIEW_LOCK:
        if path.exists():
            return path
        tmp = elevenlabs_tts.PREVIEW_DIR / f"std-{style_key}.tmp.mp3"
        try:
            elevenlabs_tts.PREVIEW_DIR.mkdir(parents=True,
                                             exist_ok=True)
            tts.synthesize_sample(elevenlabs_tts.SAMPLE_TEXT,
                                  style_key, tmp)
            tmp.replace(path)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            return None
    return path if path.exists() else None


_EL_PREVIEW_LOCK = threading.Lock()


def _ensure_el_preview(voice_id):
    """Lazily synthesize a REGISTRY voice's preview clip ONCE
    (build 214) — the bulk import stores entries without
    generating previews, so the first request for the clip
    makes it, exactly like the built-in std- clips. Only ids
    in the registry ever generate (an unknown id is a plain
    404 and never costs an API call). Returns the path, or
    None when generation failed (no key / API error) — the
    caller then serves the same plain 404 as a missing clip,
    and a later request may retry."""
    path = elevenlabs_tts.preview_path_el(voice_id)
    if path.exists():
        return path
    if not elevenlabs_tts.find_voice(voice_id):
        return None
    with _EL_PREVIEW_LOCK:
        if path.exists():
            return path
        elevenlabs_tts.generate_preview(voice_id)
    return path if path.exists() else None


_GM_PREVIEW_LOCK = threading.Lock()


def _ensure_gm_preview(vname):
    """Lazily synthesize a Gemini voice's preview clip ONCE
    (build 229) — the same first-play pattern as the std-/el-
    clips, and for the same reason: 30 voices are never
    bulk-generated (free-tier quota). Only names in the
    30-voice table ever generate (an unknown name is a plain
    404 and never costs an API call). Returns the path, or
    None when generation failed (no key / every key down) —
    the caller then serves the same plain 404 as a missing
    clip, and a later request may retry."""
    path = gemini_tts.preview_path_gm(vname)
    if path.exists():
        return path
    if not gemini_tts.find_voice(vname):
        return None
    with _GM_PREVIEW_LOCK:
        if path.exists():
            return path
        gemini_tts.generate_preview(vname)
    return path if path.exists() else None


@app.get("/voice-previews/<name>")
def voice_preview_file(name):
    """Serve a voice-library preview clip (build 207). Strict
    filename shape — el-<voice id>.mp3 / std-<style key>.mp3 /
    usr-<voice id>.mp3 (build 212 user clones) only, no
    traversal. Built-in clips generate lazily on first
    request; registry el- clips do too since build 214 (the
    bulk import never generates); usr- clips exist only if
    one was generated, so a missing file is a plain 404."""
    if not re.fullmatch(r"(?:el|std|usr|gm)-[A-Za-z0-9_-]{1,64}\.mp3",
                        name or ""):
        return jsonify({"error": "မရှိပါ"}), 404
    path = elevenlabs_tts.PREVIEW_DIR / name
    if not path.exists() and name.startswith("std-"):
        _ensure_std_preview(name[4:-4])
    elif not path.exists() and name.startswith("el-"):
        _ensure_el_preview(name[3:-4])
    elif not path.exists() and name.startswith("gm-"):
        _ensure_gm_preview(name[3:-4])
    if not path.exists():
        return jsonify({"error": "မရှိပါ"}), 404
    return send_file(path, mimetype="audio/mpeg")


# ---------- Voice Clone, self-serve (build 212) ----------
# ကိုယ်ပိုင် အသံ = the voice an ACCOUNT HOLDER records
# themselves (owner's definition, 2026-11-16). A logged-in
# user uploads ONE sample of their own voice; the server
# creates an ElevenLabs IVC clone under the OWNER's key and
# records ownership in the accounts clone store. The sample
# itself is validated, sent to ElevenLabs, and DELETED — it
# is never stored, never served, and its temp dir sits outside
# every public route. While no key is configured the whole
# feature is an honest locked state (the Clone view shows a
# lock card, the API refuses politely) and it activates with
# the key alone — no deploy.

MAX_USER_CLONES = 3
CLONE_SAMPLE_MAX_BYTES = 25 * 1024 * 1024
CLONE_MIN_SECONDS = 10.0
CLONE_MAX_SECONDS = 180.0
CLONE_TMP_DIR = ROOT / "clone_tmp"
CLONE_TMP_DIR.mkdir(exist_ok=True)
_CLONE_EXTS = (".mp3", ".m4a", ".wav", ".ogg", ".oga", ".webm",
               ".mp4", ".aac", ".flac", ".opus")


def _clone_view(c):
    return {"voice_id": c["voice_id"], "name": c["name"],
            "preview": elevenlabs_tts.preview_url_usr(c["voice_id"]),
            "created_at": c["created_at"]}


def _clone_sample_error(path):
    """Validate an uploaded clone sample -> a Burmese error
    string, or None when the sample is usable: it must decode
    as audio, run 10 s – 3 min, and carry measurable speech
    energy (a silent or near-silent file would clone into a
    voice that says nothing)."""
    from pipeline.common import probe_streams
    try:
        streams = probe_streams(path)
        dur = probe_duration(path)
    except Exception:
        return "အသံဖိုင် မဟုတ်ပါ — အသံဖိုင် အစစ် တင်ပေးပါ"
    if not any(s.get("codec_type") == "audio" for s in streams):
        return "အသံဖိုင် မဟုတ်ပါ — အသံဖိုင် အစစ် တင်ပေးပါ"
    if dur < CLONE_MIN_SECONDS:
        return ("နမူနာ အသံ တိုလွန်းပါတယ် — အနည်းဆုံး "
                "၁၀ စက္ကန့် ပြောထားတဲ့ အသံ တင်ပေးပါ")
    if dur > CLONE_MAX_SECONDS:
        return ("နမူနာ အသံ ရှည်လွန်းပါတယ် — အများဆုံး "
                "၃ မိနစ် အတွင်း ဖြတ်ပြီး တင်ပေးပါ")
    try:
        done = sh(["ffmpeg", "-v", "info", "-i", str(path),
                   "-af", "volumedetect", "-f", "null", "-"])
        text = (done.stderr or "") + (done.stdout or "")
        m_mean = re.search(r"mean_volume:\s*(-?[\d.]+|-inf) dB", text)
        m_max = re.search(r"max_volume:\s*(-?[\d.]+|-inf) dB", text)
        mean = float(m_mean.group(1)) if m_mean else float("-inf")
        peak = float(m_max.group(1)) if m_max else float("-inf")
    except Exception:
        return "အသံကို စစ်လို့ မရပါ — တခြား အသံဖိုင် စမ်းကြည့်ပါ"
    if mean <= -55.0 or peak <= -40.0:
        return ("အသံ မကြားရပါ — တိတ်ဆိတ်နေတဲ့ ဖိုင် ဖြစ်နေပါတယ်။ "
                "ပုံမှန် အသံနဲ့ ပြောထားတဲ့ နမူနာ ပြန်တင်ပေးပါ")
    return None


@app.get("/api/clones")
def clones_list():
    user = _current_user()
    clones = [_clone_view(c) for c in accounts.clones_for(user["id"])] \
        if user and user.get("id") else []
    return jsonify({"configured": elevenlabs_tts.key_configured(),
                    "cap": MAX_USER_CLONES, "clones": clones})


@app.post("/api/clones")
def clones_create():
    user = _current_user()
    if not user or not user.get("id"):
        return jsonify({"error": "အရင် login ဝင်ပါ"}), 401
    if not elevenlabs_tts.key_configured():
        return jsonify({"error":
                        "ElevenLabs မချိတ်ရသေးပါ — admin က key "
                        "ထည့်ပြီးမှ clone လုပ်လို့ရပါမယ်"}), 400
    name = (request.form.get("name") or "").strip()
    if not name or len(name) > 60 or "\n" in name \
            or any(ch in name for ch in '<>&"'):
        return jsonify({"error": "အသံ နာမည် ထည့်ပေးပါ "
                                 "(အက္ခရာ ၆၀ အတွင်း)"}), 400
    if (request.form.get("consent") or "") not in \
            ("1", "true", "on", "yes"):
        return jsonify({"error":
                        "«ဒါ ကျွန်တော့်/ကျွန်မ ကိုယ်ပိုင်အသံပါ» ကို "
                        "အတည်ပြုပေးပါ"}), 400
    if accounts.clone_count(user["id"]) >= MAX_USER_CLONES:
        return jsonify({"error":
                        f"Clone အသံ {MAX_USER_CLONES} ခု ပြည့်နေပါပြီ — "
                        "အရင် တစ်ခု ဖျက်ပြီးမှ အသစ် ထည့်လို့ရပါမယ်"}), 400
    f = request.files.get("sample")
    if f is None or not f.filename:
        return jsonify({"error": "နမူနာ အသံဖိုင် ရွေးပေးပါ"}), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in _CLONE_EXTS:
        ext = ".audio"
    tmp = CLONE_TMP_DIR / (uuid.uuid4().hex + ext)
    try:
        size = 0
        with open(tmp, "wb") as out:
            while True:
                chunk = f.stream.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > CLONE_SAMPLE_MAX_BYTES:
                    return jsonify({"error":
                                    "ဖိုင် ကြီးလွန်းပါတယ် — "
                                    "အများဆုံး 25 MB ပဲ ရပါတယ်"}), 400
                out.write(chunk)
        err = _clone_sample_error(tmp)
        if err:
            return jsonify({"error": err}), 400
        try:
            voice_id = elevenlabs_tts.add_voice(name, tmp)
        except RuntimeError as e:
            return jsonify({"error": str(e)}), 400
        try:
            row = accounts.add_clone(user["id"], voice_id, name)
        except Exception:
            # The ElevenLabs voice exists but the ownership row
            # did not land — remove the orphan so it never leaks
            # a slot in the owner's account.
            try:
                elevenlabs_tts.delete_voice(voice_id)
            except Exception:
                pass
            return jsonify({"error":
                            "Clone သိမ်းတာ မရပါ — ခဏနေမှ "
                            "ပြန်စမ်းပါ"}), 400
        elevenlabs_tts.generate_preview_usr(voice_id)
        return jsonify({"clone": _clone_view(row),
                        "clones": [_clone_view(c) for c in
                                   accounts.clones_for(user["id"])]}), 201
    finally:
        # The sample is the user's private recording: whatever
        # happened above, it does not stay on this server.
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


@app.delete("/api/clones/<voice_id>")
def clones_delete(voice_id):
    user = _current_user()
    if not user or not user.get("id"):
        return jsonify({"error": "အရင် login ဝင်ပါ"}), 401
    row = accounts.clone_by_voice(voice_id)
    if row is None or row["user_id"] != user["id"]:
        return jsonify({"error": "မရှိပါ"}), 404
    try:
        elevenlabs_tts.delete_voice(voice_id)
    except RuntimeError as e:
        # ElevenLabs still holds the voice — keep the row so
        # the slot is accounted for and the user can retry.
        return jsonify({"error": str(e)}), 400
    accounts.delete_clone(user["id"], voice_id)
    try:
        elevenlabs_tts.preview_path_usr(voice_id).unlink(
            missing_ok=True)
    except Exception:
        pass
    return jsonify({"clones": [_clone_view(c) for c in
                               accounts.clones_for(user["id"])]})


def _public_url_ok(url):
    """SSRF guard: downloads run server-side, so a customer-supplied
    link must resolve to PUBLIC addresses only — no loopback,
    private, link-local, reserved or multicast targets (a hostname
    with ANY bad record is refused whole)."""
    import ipaddress
    import socket
    from urllib.parse import urlparse
    try:
        host = urlparse(url).hostname
        if not host:
            return False
        for info in socket.getaddrinfo(host, None,
                                       proto=socket.IPPROTO_TCP):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local \
                    or ip.is_reserved or ip.is_multicast \
                    or ip.is_unspecified:
                return False
        return True
    except Exception:
        return False


def _src_valid(path):
    """A cached preview source is reusable only as a COMPLETE,
    probeable video — a truncated file left by an interrupted
    download must never poison the cache for every later job on
    the same link."""
    try:
        return path.exists() and path.stat().st_size > 50_000 \
            and probe_duration(path) > 0
    except Exception:
        return False


_PREVIEW_HITS = {}


def _preview_allowed():
    """Previews fetch arbitrary URLs on the server's bandwidth:
    cap them per user (or per IP when accounts are off)."""
    me = _current_user()
    key = f"u{me['id']}" if me else f"ip:{_client_ip()}"
    now = time.time()
    hits = [t for t in _PREVIEW_HITS.get(key, []) if now - t < 3600]
    if len(hits) >= 30:
        _PREVIEW_HITS[key] = hits
        return False
    hits.append(now)
    _PREVIEW_HITS[key] = hits
    return True


def _preview_dir(url, quality=None):
    """Preview cache identity (build 203, audit V08).

    With a quality, the pid hashes URL + quality, so an hd entry
    and a 480p entry for the same link are independent cache
    entries that can never evict or overwrite each other (before,
    the URL-only key made alternating-quality requests unlink and
    re-download over one shared file). Without a quality this
    returns the LEGACY pre-203 pid — kept deliberately as the
    read path for entries cached before build 203 and as the
    bot prepare flow's own key (the bot downloads before the
    customer picks a quality, and _preview_quality() on the
    entry's source_info.json still gates job reuse). The "\\n"
    separator cannot appear in a cleaned URL, so a keyed pid can
    never collide with a legacy one."""
    if quality:
        pid = hashlib.sha256(
            f"{url}\n{quality}".encode("utf-8")).hexdigest()[:12]
    else:
        pid = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return pid, PREVIEWS_DIR / pid


def _preview_dirs_for(url):
    """Every cache dir a URL may own: the legacy entry plus one
    keyed entry per quality (deduped, order-stable)."""
    out, seen = [], set()
    for q in (None, "hd", "720", "480"):
        pid, pdir = _preview_dir(url, q)
        if pid not in seen:
            seen.add(pid)
            out.append((pid, pdir))
    return out


# Per-key preview locks (build 203, audit V08): the same
# lock-dict pattern as pipeline/telegram_send._chat_lock. One
# lock per preview pid serializes that entry's whole
# check -> download -> publish sequence, so two concurrent
# previews of the same URL+quality coalesce onto ONE download
# instead of both unlinking and streaming into the same file.
_PREVIEW_LOCKS = {}
_PREVIEW_LOCKS_G = threading.Lock()


def _preview_lock(pid):
    with _PREVIEW_LOCKS_G:
        lk = _PREVIEW_LOCKS.get(pid)
        if lk is None:
            lk = _PREVIEW_LOCKS[pid] = threading.Lock()
        return lk


def _active_preview_dirs():
    """Preview dirs an ACTIVE (queued/running) job still depends
    on via its source_path. Cleanup and the sweeper must never
    remove these (build 203, audit V08): before, one job's
    cleanup could rmtree the shared preview dir out from under
    another job that had not reached phase 1 yet."""
    out = set()
    for j in list(JOBS.values()):
        if j.get("status") in ("queued", "running"):
            sp = j.get("source_path")
            if sp:
                out.add(str(Path(sp).parent))
    return out


def _preview_quality(pdir):
    """Which download quality a cached preview was fetched at
    (build 167). download() records it in source_info.json; a
    preview cached before build 167 has no record and was fetched
    with the old light (<=720p) selectors, so it counts as "720".
    A job may reuse the cached file ONLY when this matches the
    quality the customer picked — otherwise phase 1 downloads
    fresh at the picked quality."""
    try:
        meta = json.loads((Path(pdir) / "source_info.json")
                          .read_text(encoding="utf-8"))
        q = meta.get("quality")
        if q in ("hd", "720", "480"):
            return q
    except Exception:
        pass
    return "720"


# Build 202 (audit V05): preview work is BOUNDED. A preview used
# to run a full download synchronously inside the request, outside
# the job queue — enough slow previews could occupy all 8 Waitress
# threads and stall logins. Now at most _PREVIEW_MAX_CONCURRENT
# preview downloads run at once (overflow gets an immediate 429),
# each download runs under a hard total deadline, and a source
# longer than _PREVIEW_MAX_DURATION_S is refused as a preview
# (jobs themselves have no such ceiling — the wizard's skip
# button still lets the customer submit the job directly). The
# per-user 30/hour admission cap and the login gate are unchanged.
_PREVIEW_SEM = threading.Semaphore(2)
_PREVIEW_DEADLINE_S = 180
_PREVIEW_MAX_DURATION_S = 10800  # 3 hours (build 241: was 60 min —
# ~2-hour dramas are the core content, so the old cap refused the
# main use. The cap bounds only this DOWNLOAD path; uploaded files
# take /api/frame-upload, which never had a duration cap — the
# file is already local and an ffmpeg seek is O(1) in duration)


def _preview_download_bounded(url, pdir, quality):
    """Run one preview download on a worker thread under the
    total deadline. Returns ("ok", None), ("timeout", None) or
    ("error", exception).

    A Python thread cannot be killed, so on timeout the worker
    may run on in the background; the caller passes a STAGING
    dir (build 203), so its writes stay inside that staging dir
    — the caller removes it and releases the slot, and a
    late-finishing worker can only ever write into the staging
    dir (at worst recreating it as garbage the startup sweep
    collects); it can never touch the published cache entry.
    The download itself is bounded by the V04 byte cap plus
    download()'s per-request socket timeouts."""
    box = {}

    def _go():
        try:
            dl_mod.download(url, pdir, quality=quality)
        except Exception as e:  # surfaced by the caller
            box["err"] = e

    t = threading.Thread(target=_go, daemon=True)
    t.start()
    t.join(_PREVIEW_DEADLINE_S)
    if t.is_alive():
        return "timeout", None
    if "err" in box:
        return "error", box["err"]
    return "ok", None


@app.post("/api/preview")
def make_preview():
    """Download (once, cached by URL+quality) and report
    size/duration for the editor."""
    data = request.get_json(force=True)
    url = dl_mod.clean_url(data.get("url") or "")
    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "လင့်ခ် မမှန်ပါ"}), 400
    if not _public_url_ok(url):
        return jsonify({"error": "ဒီ လင့်ခ် ကို သုံးလို့ မရပါ"}), 400
    if not _preview_allowed():
        return jsonify({"error": "ခဏ ခဏ မစမ်းပါနဲ့ — ခဏ စောင့်ပြီး ပြန် စမ်းပါ"}), 429
    # Build 183: the wizard already knows the quality the customer
    # picked on this step — fetch the preview AT that quality so
    # the cached file is exactly the rendition the eventual job
    # will reuse (before, the preview always pulled the hd master
    # and a 720/480 job then downloaded a second time). A cached
    # preview fetched at a DIFFERENT quality is re-fetched at the
    # requested one; the job-side quality check in create_job
    # stays as the guarantee when the two still differ.
    quality = dl_mod.norm_quality(data.get("quality"))
    pid, pdir = _preview_dir(url, quality)
    # Build 203 (audit V08): the whole check -> download ->
    # publish sequence runs under this entry's own lock, so
    # parallel previews of the same URL+quality coalesce onto
    # ONE download (the second caller waits, then hits the
    # fresh entry); different qualities hold different locks
    # and different dirs and never disturb each other.
    with _preview_lock(pid):
        serve_pid, serve_dir = pid, pdir
        src = pdir / "source.mp4"
        try:
            if not _src_valid(src):
                # Deliberate legacy read path: an entry cached
                # before build 203 (or by the bot prepare flow)
                # under the URL-only key still serves when it
                # was fetched at THIS quality. Read-only here —
                # a quality mismatch downloads into the keyed
                # entry and leaves the legacy entry intact for
                # its own quality's callers (no more thrash).
                lpid, ldir = _preview_dir(url)
                if ldir != pdir \
                        and _src_valid(ldir / "source.mp4") \
                        and _preview_quality(ldir) == quality:
                    serve_pid, serve_dir = lpid, ldir
                    src = ldir / "source.mp4"
            if serve_dir == pdir and not _src_valid(src):
                # Build 202 (audit V05): only a couple of preview
                # downloads may run at once — the rest of the web
                # threads stay free for logins and status calls.
                if not _PREVIEW_SEM.acquire(blocking=False):
                    return jsonify({"error": "Preview အလုပ် များနေပါတယ် — "
                                             "ခဏ စောင့်ပြီး ပြန် စမ်းပါ"}), 429
                # Download into a staging sibling dir; the entry
                # at the key is replaced only once a complete
                # download exists (atomic publish below), so a
                # failed or interrupted download never destroys
                # a previously valid entry.
                staging = PREVIEWS_DIR / (
                    pid + ".tmp-" + uuid.uuid4().hex[:8])
                staging.mkdir(parents=True, exist_ok=True)
                try:
                    state, err = _preview_download_bounded(
                        url, staging, quality)
                finally:
                    _PREVIEW_SEM.release()
                if state == "timeout":
                    # The still-running worker writes only into
                    # the staging dir; drop it and report. The
                    # slot is already released.
                    shutil.rmtree(staging, ignore_errors=True)
                    return jsonify({"error": "Preview ဒေါင်းတာ အချိန် "
                                             "ကုန်သွားပါတယ် — ခဏနေပြီး "
                                             "ပြန် စမ်းပါ"}), 504
                if state == "error":
                    shutil.rmtree(staging, ignore_errors=True)
                    raise err
                shutil.rmtree(pdir, ignore_errors=True)
                os.rename(staging, pdir)
                src = pdir / "source.mp4"
            dur = probe_duration(src)
            if dur > _PREVIEW_MAX_DURATION_S:
                # A preview exists to pick a blur spot; an overlong
                # source is refused (and not kept cached) — the job
                # itself is unaffected and can be submitted directly.
                src.unlink(missing_ok=True)
                (serve_dir / "source_info.json").unlink(missing_ok=True)
                return jsonify({"error": "ဒီ video က အရမ်း ရှည်နေလို့ "
                                         "preview မရပါ (အများဆုံး "
                                         "၃ နာရီ) — preview မပါဘဲ job "
                                         "တိုက်ရိုက် တင်လို့ ရပါတယ်"}), 400
            w, h = fx_mod.video_size(src)
        except Exception as e:
            # Build 166 (fix F1): polite Burmese category to the
            # customer; the raw error is logged server-side only.
            print(f"[preview] FAILED {url}: {str(e)[:400]}", flush=True)
            return jsonify({"error": dl_mod.polite_error(e, url)}), 502
    title = None
    info_p = serve_dir / "source_info.json"
    if info_p.exists():
        try:
            title = json.loads(info_p.read_text(encoding="utf-8")).get("title")
        except Exception:
            pass
    return jsonify({"preview_id": serve_pid, "duration": round(dur, 2),
                    "width": w, "height": h, "title": title,
                    "quality": quality})


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


@app.post("/api/frame-upload")
def frame_upload():
    """Build 226 (audit F-04): ONE frame from an uploaded
    video file, for the editors (the dub wizard, Recap,
    Commentary) when the source is a file instead of a link —
    the link path draws on /api/preview frames, but an upload
    had no frame at all, so blur/logo boxes were placed
    blind. Session-authenticated like every route here (the
    accounts guard covers /api/*), rate-capped like previews;
    the file passes the upload extension whitelist + a real
    probe, serves this one grab (preview_frame's ffmpeg
    verbatim), and is deleted — nothing is kept. Duration and
    dimensions ride in headers for the editor. Build 242: an
    upload_token form field names a finished chunked upload
    instead of the file part — the grab reads the staged
    file in place and keeps it (only a job spends a token)."""
    if not _preview_allowed():
        print("[frame-upload] FAILED rate cap hit", flush=True)
        return jsonify({"error": "ခဏ ခဏ မစမ်းပါနဲ့ — ခဏ စောင့်ပြီး "
                                 "ပြန် စမ်းပါ"}), 429
    f = request.files.get("file")
    staged = None
    if f is None or not (f.filename or "").strip():
        # Build 242: the file may already be on the server
        # as a finished chunked upload — the token names it
        # and the grab below reads the staged file IN PLACE
        # (no copy, and a frame grab never spends the token;
        # the job that follows is what spends it).
        token = (request.form.get("upload_token") or "").strip()
        if token:
            staged = _upload_for_token(token, _current_user())
            if staged is None:
                print("[frame-upload] FAILED upload token "
                      "unresolvable", flush=True)
                return jsonify({"error": "တင်ထားတဲ့ ဖိုင် "
                                         "မတွေ့တော့ပါ — ဖိုင် "
                                         "ပြန် တင်ပါ"}), 400
        if staged is None:
            print("[frame-upload] FAILED no file in request", flush=True)
            return jsonify({"error": "ဖိုင် မပါပါ"}), 400
    if staged is not None:
        name = staged["name"]
        ext = staged["ext"]
    else:
        name = (f.filename or "").strip()
        ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in UPLOAD_EXTS:
        print(f"[frame-upload] FAILED bad extension {name!r} "
              f"ext={ext!r}", flush=True)
        return jsonify({"error": "ဒီ ဖိုင် အမျိုးအစား မရပါ — mp4, mkv, "
                                 "mov, avi, webm ဖိုင် ပဲ ရပါတယ်"}), 400
    tmp = PREVIEWS_DIR / ("frameup-" + uuid.uuid4().hex[:12])
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        if staged is not None:
            dest = Path(staged["path"])
        else:
            dest = tmp / ("upload" + ext)
            total = 0
            too_big = False
            with open(dest, "wb") as out:
                while True:
                    chunk = f.stream.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > dl_mod.MAX_DOWNLOAD_BYTES:
                        too_big = True
                        break
                    out.write(chunk)
            if too_big:
                print(f"[frame-upload] FAILED too big {name!r} "
                      f"bytes>{dl_mod.MAX_DOWNLOAD_BYTES}", flush=True)
                return jsonify({"error": "ဖိုင် အရွယ် ကြီးလွန်းပါတယ် — "
                                         "အများဆုံး 2 GB ပဲ ရပါတယ်"}), 400
        probe_err = ""
        try:
            dur = probe_duration(dest)
            w, h = fx_mod.video_size(dest)
        except Exception as e:
            dur, w, h = 0, 0, 0
            probe_err = str(e)[:200]
        if not dur or dur <= 0 or not w:
            # Build 241: log it — this return used to be the only
            # trace of an unreadable upload, and the client could
            # not tell it apart from any other preview failure.
            print(f"[frame-upload] FAILED unreadable {name}: "
                  f"dur={dur} w={w} err={probe_err}", flush=True)
            return jsonify({"error": "ဖိုင် ကို ဗီဒီယို အဖြစ် ဖတ်လို့ "
                                     "မရပါ — ဗီဒီယို ဖိုင် အမှန် "
                                     "ပြန်တင်ပါ"}), 400
        try:
            t = float(request.form.get("t") or 2)
        except (TypeError, ValueError):
            t = 2.0
        t = min(max(t, 0.0), max(dur - 0.15, 0.0))
        frame = tmp / "frame.jpg"
        try:
            sh(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}",
                "-i", str(dest), "-frames:v", "1", "-q:v", "3",
                str(frame)])
            payload = frame.read_bytes()
        except Exception as e:
            # Build 241: a failed grab used to escape as a bare
            # Flask 500 (an HTML page), which the wizard could
            # only collapse into the generic "preview မရပါ".
            # Log it server-side and answer JSON like every
            # other failure on this route.
            print(f"[frame-upload] FAILED frame grab {name}: "
                  f"{str(e)[:300]}", flush=True)
            return jsonify({"error": "ဒီ ဖိုင်ကနေ preview ပုံ "
                                     "ယူလို့ မရပါ — ဖိုင် ပြန်စစ်ပြီး "
                                     "ပြန် စမ်းပါ"}), 500
        resp = Response(payload, mimetype="image/jpeg")
        resp.headers["X-Video-Duration"] = f"{dur:.2f}"
        resp.headers["X-Video-Width"] = str(w)
        resp.headers["X-Video-Height"] = str(h)
        return resp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
    # Build 209: multi-region blur. fx_in["blur_boxes"] (a list,
    # count capped in _new_job) normalizes into fx["blur_boxes"],
    # and fx["blur"] mirrors box #1 — the primary — so the
    # subtitle-placement rule (build 123) and every pre-209
    # reader keep working unchanged. Entries that fail to parse
    # or have no area are dropped. Without a list, the legacy
    # single fx_in["blur"] path below is byte-identical to
    # build 208.
    boxes = []
    if isinstance(fx_in.get("blur_boxes"), list):
        for b in fx_in["blur_boxes"]:
            if not isinstance(b, dict):
                continue
            try:
                box = {"x": float(b["x"]), "y": float(b["y"]),
                       "w": float(b["w"]), "h": float(b["h"]),
                       "strength": float(b.get("strength") or 20)}
            except (KeyError, TypeError, ValueError):
                continue
            if box["w"] <= 0 or box["h"] <= 0:
                continue
            boxes.append(box)
    blur = fx_in.get("blur")
    if boxes:
        fx["blur_boxes"] = boxes
        fx["blur"] = boxes[0]
    elif blur:
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


def _karaoke_from_data(data):
    """Build 231: the karaoke tick, one parse for every create
    (web JSON True, form "1"/"true"/"on"/"yes"). The Captions
    create has carried this flag since build 220; 231 extends
    it to Dub/Recap/Commentary with the same semantics."""
    v = (data or {}).get("karaoke")
    return v is True or (isinstance(v, str)
                         and v.strip().lower()
                         in ("1", "true", "on", "yes"))


def _fx_from_data(data):
    """Build 226: the fx for the products that gained the blur
    editor in this build (Recap / Commentary / Captions and
    the v1 API) — the SAME _build_fx normalizer the dub wizard
    uses, with the build-209 box cap enforced the same way
    _new_job enforces it. Returns (fx_or_None, error_or_None):
    fx is None when the payload carries no usable effect, so
    a job without blur keeps the exact pre-226 record shape.
    Before 226 these products hardcoded "fx": None at creation
    and again in their phase-2 workers, so a customer's boxes
    could never reach the render (audit F-01/F-02/F-03)."""
    _boxes_in = (data.get("fx") or {}).get("blur_boxes")
    if isinstance(_boxes_in, list) \
            and len(_boxes_in) > fx_mod.MAX_BLUR_BOXES:
        return None, ("ဝါးမည့် အကွက် အများဆုံး ၅ ခု ပဲ ရပါတယ်"
                      " — အကွက် လျှော့ပြီး ပြန်စမ်းပါ")
    fx = _build_fx(data)
    return (fx if fx_mod.has_fx(fx) else None), None


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
    # Build 229: the status reports COUNTS only (pool size,
    # disabled keys, the voice rate) — key values are never
    # returned, exactly as before.
    return jsonify({
        "configured": gemini_pool.key_configured(),
        "count": gemini_pool.key_count(),
        "disabled": gemini_pool.disabled_count(),
        "credits_per_min": gemini_tts.credits_per_min()})


@app.post("/api/settings/gemini-key")
def gemini_key_save():
    # Build 229: the card saves a POOL — up to 30 keys, bulk
    # paste one per line (a one-line paste is a pool of one,
    # the pre-229 behaviour). Both Gemini key formats are
    # valid: legacy "AIza…" keys and the "AQ.…" auth keys AI
    # Studio has issued since May 2026 (~53 chars) — the
    # format rule lives in gemini_pool.parse_keys. The pool
    # replaces the old value wholesale (the card's standing
    # rule) and clears any disabled marks: a fresh paste is
    # a fresh start.
    data = request.get_json(force=True) or {}
    raw = data.get("keys") if "keys" in data else data.get("key")
    keys, err = gemini_pool.parse_keys(raw)
    if err:
        return jsonify({"error": err}), 400
    _env_write("GEMINI_API_KEYS", ",".join(keys))
    _env_remove("GEMINI_API_KEY")
    gemini_pool.reset_disabled()
    return jsonify({"ok": True, "configured": True,
                    "count": len(keys), "disabled": 0,
                    "credits_per_min": gemini_tts.credits_per_min()})


@app.delete("/api/settings/gemini-key")
def gemini_key_delete():
    _env_remove("GEMINI_API_KEYS")
    _env_remove("GEMINI_API_KEY")
    gemini_pool.reset_disabled()
    return jsonify({"ok": True, "configured": False,
                    "count": 0, "disabled": 0})


@app.post("/api/settings/gemini")
def gemini_save():
    """Gemini voice settings (build 229) — partial saves in
    the elevenlabs_save pattern: each PRESENT field is
    validated then written. Today: the per-minute credit
    rate for Gemini voices (GEMINI_CREDITS_PER_MIN)."""
    data = request.get_json(force=True) or {}
    writes = {}
    if "credits_per_min" in data:
        try:
            rate = int(data["credits_per_min"])
        except (TypeError, ValueError):
            return jsonify({"error": "credit နှုန်း မမှန်ပါ"}), 400
        if not 1 <= rate <= 100:
            return jsonify({"error": "credit နှုန်း 1–100 အတွင်း ဖြစ်ရပါမယ်"}), 400
        writes["GEMINI_CREDITS_PER_MIN"] = str(rate)
    if not writes:
        return jsonify({"error": "သိမ်းစရာ ဆက်တင် မပါပါ"}), 400
    for k, v in writes.items():
        _env_write(k, v)
    return jsonify({"ok": True,
                    "credits_per_min": gemini_tts.credits_per_min()})


# ---------- OpenAI key (server-side only, same rules as Gemini) ----------

@app.get("/api/settings/openai-key")
def openai_key_status():
    from pipeline import openai_translate
    return jsonify({"configured": openai_translate.key_configured()})


@app.post("/api/settings/openai-key")
def openai_key_save():
    data = request.get_json(force=True) or {}
    key = (data.get("key") or "").strip()
    ok_format = key.startswith("sk-") and 20 <= len(key) <= 200 \
        and all(ch.isalnum() or ch in "._-" for ch in key)
    if not ok_format:
        return jsonify({"error": "key မမှန်ပါ — OpenAI key (sk- နဲ့ စတာ) ဖြစ်ရပါမယ်"}), 400
    _env_write("OPENAI_API_KEY", key)
    return jsonify({"ok": True, "configured": True})


@app.delete("/api/settings/openai-key")
def openai_key_delete():
    _env_remove("OPENAI_API_KEY")
    return jsonify({"ok": True, "configured": False})


# ---------- OpenRouter key (server-side only, same rules) ----------

@app.get("/api/settings/openrouter-key")
def openrouter_key_status():
    from pipeline import openrouter_translate
    return jsonify({"configured": openrouter_translate.key_configured()})


@app.post("/api/settings/openrouter-key")
def openrouter_key_save():
    data = request.get_json(force=True) or {}
    key = (data.get("key") or "").strip()
    ok_format = key.startswith("sk-or-") and 20 <= len(key) <= 200 \
        and all(ch.isalnum() or ch in "._-" for ch in key)
    if not ok_format:
        return jsonify({"error": "key မမှန်ပါ — OpenRouter key (sk-or- နဲ့ စတာ) ဖြစ်ရပါမယ်"}), 400
    _env_write("OPENROUTER_API_KEY", key)
    return jsonify({"ok": True, "configured": True})


@app.delete("/api/settings/openrouter-key")
def openrouter_key_delete():
    _env_remove("OPENROUTER_API_KEY")
    return jsonify({"ok": True, "configured": False})


@app.get("/api/settings/xkiro-key")
def xkiro_key_status():
    from pipeline import xkiro_translate
    return jsonify({"configured": xkiro_translate.key_configured()})


@app.post("/api/settings/xkiro-key")
def xkiro_key_save():
    data = request.get_json(force=True) or {}
    key = (data.get("key") or "").strip()
    ok_format = 20 <= len(key) <= 200 \
        and all(ch.isalnum() or ch in "._-" for ch in key)
    if not ok_format:
        return jsonify({"error": "key မမှန်ပါ — xKiro key ဖြစ်ရပါမယ်"}), 400
    _env_write("XKIRO_API_KEY", key)
    return jsonify({"ok": True, "configured": True})


@app.delete("/api/settings/xkiro-key")
def xkiro_key_delete():
    _env_remove("XKIRO_API_KEY")
    return jsonify({"ok": True, "configured": False})


# ---------- ElevenLabs premium voices (build 205) ----------
# The owner's ElevenLabs key + voice registry live in .env
# exactly like the other keys (chmod 600, never in git). The key
# value is NEVER returned — only whether one is configured. Voice
# ids + display names are not secrets and ARE returned, but only
# through these /api/settings/* routes, which the accounts guard
# restricts to admins; customers never see them here (they see
# the names alone, in the picker, and only when configured).

def _el_status_voices():
    """Registry entries for the Admin card (build 207): tag/desc
    always present, plus whether a preview clip exists."""
    return [{**v, "tag": v.get("tag", ""), "desc": v.get("desc", ""),
             "preview": elevenlabs_tts.preview_path_el(
                 v["id"]).exists()}
            for v in elevenlabs_tts.get_voices()]


@app.get("/api/settings/elevenlabs")
def elevenlabs_status():
    return jsonify({
        "configured": elevenlabs_tts.key_configured(),
        "model": elevenlabs_tts.get_model(),
        "credits_per_min": elevenlabs_tts.credits_per_min(),
        "access": elevenlabs_tts.get_access_mode(),
        "test_users": elevenlabs_tts.get_test_users(),
        "commentary_credits_per_min": _commentary_credits_per_min(),
        "captions_credits_per_min": _captions_credits_per_min(),
        "download_credits_per_min": _download_credits_per_min(),
        "referral_reward_credits": _referral_reward_credits(),
        "voices": _el_status_voices()})


@app.post("/api/settings/elevenlabs-key")
def elevenlabs_key_save():
    data = request.get_json(force=True) or {}
    key = (data.get("key") or "").strip()
    ok_format = key.startswith("sk_") and 20 <= len(key) <= 200 \
        and all(ch.isalnum() or ch in "._-" for ch in key)
    if not ok_format:
        return jsonify({"error": "key မမှန်ပါ — ElevenLabs key (sk_ နဲ့ စတာ) ဖြစ်ရပါမယ်"}), 400
    _env_write("ELEVENLABS_API_KEY", key)
    return jsonify({"ok": True, "configured": True})


@app.delete("/api/settings/elevenlabs-key")
def elevenlabs_key_delete():
    _env_remove("ELEVENLABS_API_KEY")
    return jsonify({"ok": True, "configured": False})


@app.post("/api/settings/elevenlabs")
def elevenlabs_save():
    # Build 206: partial saves — each PRESENT field is validated
    # then written; absent fields keep their saved values. The
    # card sends all four (model, rate, access, test users) at
    # once; build 205's model+rate-only posts keep working.
    data = request.get_json(force=True) or {}
    writes = {}
    if "model" in data:
        if data["model"] not in elevenlabs_tts.MODELS:
            return jsonify({"error": "model မမှန်ပါ"}), 400
        writes["ELEVENLABS_MODEL"] = data["model"]
    if "credits_per_min" in data:
        try:
            rate = int(data["credits_per_min"])
        except (TypeError, ValueError):
            return jsonify({"error": "credit နှုန်း မမှန်ပါ"}), 400
        if not 1 <= rate <= 100:
            return jsonify({"error": "credit နှုန်း 1–100 အတွင်း ဖြစ်ရပါမယ်"}), 400
        writes["ELEVENLABS_CREDITS_PER_MIN"] = str(rate)
    if "access" in data:
        if data["access"] not in elevenlabs_tts.ACCESS_MODES:
            return jsonify({"error": "သုံးခွင့် ပုံစံ မမှန်ပါ"}), 400
        writes["ELEVENLABS_ACCESS"] = data["access"]
    if "commentary_credits_per_min" in data:
        try:
            crate = int(data["commentary_credits_per_min"])
        except (TypeError, ValueError):
            return jsonify({"error": "credit နှုန်း မမှန်ပါ"}), 400
        if not 1 <= crate <= 100:
            return jsonify({"error":
                            "credit နှုန်း 1–100 အတွင်း "
                            "ဖြစ်ရပါမယ်"}), 400
        writes["COMMENTARY_CREDITS_PER_MIN"] = str(crate)
    if "captions_credits_per_min" in data:
        try:
            caprate = float(data["captions_credits_per_min"])
        except (TypeError, ValueError):
            return jsonify({"error": "credit နှုန်း မမှန်ပါ"}), 400
        if not 0.05 <= caprate <= 10:
            return jsonify({"error":
                            "credit နှုန်း 0.05–10 အတွင်း "
                            "ဖြစ်ရပါမယ်"}), 400
        writes["CAPTIONS_CREDITS_PER_MIN"] = str(caprate)
    if "download_credits_per_min" in data:
        try:
            dlrate = int(data["download_credits_per_min"])
        except (TypeError, ValueError):
            return jsonify({"error": "credit နှုန်း မမှန်ပါ"}), 400
        if not 1 <= dlrate <= 1000:
            return jsonify({"error":
                            "credit နှုန်း 1–1000 အတွင်း "
                            "ဖြစ်ရပါမယ်"}), 400
        writes["DOWNLOAD_CREDITS_PER_MIN"] = str(dlrate)
    if "referral_reward_credits" in data:
        try:
            refamt = int(data["referral_reward_credits"])
        except (TypeError, ValueError):
            return jsonify({"error": "credit ဆု မမှန်ပါ"}), 400
        if not 0 <= refamt <= 1000:
            return jsonify({"error":
                            "credit ဆု 0–1000 အတွင်း "
                            "ဖြစ်ရပါမယ်"}), 400
        writes["REFERRAL_REWARD_CREDITS"] = str(refamt)
    if "test_users" in data:
        users, uerr = elevenlabs_tts.parse_test_users(
            data["test_users"])
        if uerr:
            return jsonify({"error": uerr}), 400
        writes["ELEVENLABS_TEST_USERS"] = ",".join(users)
    if not writes:
        return jsonify({"error": "သိမ်းစရာ ဆက်တင် မပါပါ"}), 400
    for k, v in writes.items():
        _env_write(k, v)
    return jsonify({"ok": True,
                    "model": elevenlabs_tts.get_model(),
                    "credits_per_min":
                        elevenlabs_tts.credits_per_min(),
                    "access": elevenlabs_tts.get_access_mode(),
                    "test_users": elevenlabs_tts.get_test_users(),
                    "commentary_credits_per_min":
                        _commentary_credits_per_min(),
                    "captions_credits_per_min":
                        _captions_credits_per_min(),
                    "download_credits_per_min":
                        _download_credits_per_min(),
                    "referral_reward_credits":
                        _referral_reward_credits()})


def _el_validate_entry(data):
    """Validate one registry entry (build 214: shared by the
    single-add route and the bulk import so both apply EXACTLY
    the same rules). Returns (entry, None) or (None, error) —
    the entry stores tag/desc only when non-empty, so a plain
    {id, name} keeps the build-205 shape exactly."""
    if not isinstance(data, dict):
        return None, "entry ပုံစံ မမှန်ပါ (object ဖြစ်ရပါမယ်)"
    vid = data.get("id")
    vid = vid.strip() if isinstance(vid, str) else ""
    name = data.get("name")
    name = name.strip() if isinstance(name, str) else ""
    tag = data.get("tag")
    tag = tag.strip() if isinstance(tag, str) else ""
    desc = data.get("desc")
    desc = desc.strip() if isinstance(desc, str) else ""
    if not vid or len(vid) > 64 \
            or not all(ch.isalnum() or ch in "_-" for ch in vid):
        return None, "Voice ID မမှန်ပါ"
    if not name or len(name) > 60:
        return None, "အသံ နာမည် ထည့်ပါ (အများဆုံး 60 လုံး)"
    if len(tag) > 40:
        return None, "ပုံစံ (tag) ရှည်လွန်းပါတယ် — အများဆုံး 40 လုံး"
    if len(desc) > 160:
        return None, "ရှင်းလင်းချက် ရှည်လွန်းပါတယ် — အများဆုံး 160 လုံး"
    entry = {"id": vid, "name": name}
    if tag:
        entry["tag"] = tag
    if desc:
        entry["desc"] = desc
    return entry, None


@app.post("/api/settings/elevenlabs-voices")
def elevenlabs_voice_add():
    data = request.get_json(force=True) or {}
    entry, err = _el_validate_entry(data)
    if err:
        return jsonify({"error": err}), 400
    vid = entry["id"]
    # Build 207: re-adding an id replaces the WHOLE entry (the
    # edit path).
    voices = [v for v in elevenlabs_tts.get_voices()
              if v["id"] != vid]
    voices.append(entry)
    _env_write("ELEVENLABS_VOICES",
               json.dumps(voices, ensure_ascii=False))
    # Build 207: synthesize the library preview right away.
    # Best-effort — the voice stays saved either way; the admin
    # gets a polite note (and the regenerate button) on failure.
    preview_ok = elevenlabs_tts.generate_preview(vid)
    resp = {"ok": True, "voices": voices, "preview": preview_ok}
    if not preview_ok:
        resp["note"] = ("အသံ သိမ်းပြီးပါပြီ — ဒါပေမဲ့ preview အသံ "
                        "ထုတ်လို့ မရသေးပါ။ API key စစ်ပြီး «preview "
                        "ထုတ်ရန်» ခလုတ်နဲ့ ပြန်စမ်းနိုင်ပါတယ်")
    return jsonify(resp)


EL_REGISTRY_CAP = 200


@app.post("/api/settings/elevenlabs-voices-bulk")
def elevenlabs_voices_bulk():
    """Bulk-import registry voices (build 214): the owner
    pastes a JSON array of {id, name, tag?, desc?} entries —
    e.g. a whole sampled voice library — and every valid entry
    is merged by id (existing id replaced, new id appended).
    Invalid entries are skipped and reported per index; the
    registry is capped at EL_REGISTRY_CAP entries. NO preview
    is generated here: previews for imported voices generate
    lazily on first request (see the /voice-previews route),
    so importing 58 voices costs zero ElevenLabs calls. The
    API key is never part of the request or the response."""
    data = request.get_json(force=True, silent=True)
    if isinstance(data, dict) and isinstance(
            data.get("voices"), list):
        data = data["voices"]
    if not isinstance(data, list):
        return jsonify({"error": "JSON array ဖြစ်ရပါမယ် — "
                                 "[{\"id\": \"…\", \"name\": \"…\"}, …]"}), 400
    voices = elevenlabs_tts.get_voices()
    by_id = {v["id"]: i for i, v in enumerate(voices)}
    imported = 0
    skipped = []
    for i, raw in enumerate(data):
        entry, err = _el_validate_entry(raw)
        if err:
            skipped.append({"index": i, "reason": err})
            continue
        vid = entry["id"]
        if vid in by_id:
            voices[by_id[vid]] = entry
        elif len(voices) >= EL_REGISTRY_CAP:
            skipped.append({"index": i,
                            "reason": "အသံ စာရင်း ပြည့်နေပါပြီ "
                                      f"(အများဆုံး {EL_REGISTRY_CAP} ခု)"})
            continue
        else:
            by_id[vid] = len(voices)
            voices.append(entry)
        imported += 1
    if imported:
        _env_write("ELEVENLABS_VOICES",
                   json.dumps(voices, ensure_ascii=False))
    return jsonify({"ok": True, "imported": imported,
                    "skipped": skipped, "voices": voices})


@app.post("/api/settings/elevenlabs-voices/preview")
def elevenlabs_voice_preview_regen():
    """(Re)generate one voice's library preview clip (build
    207). Best-effort like the add-time generation: failure
    leaves the voice untouched and says so politely."""
    data = request.get_json(force=True, silent=True) or {}
    vid = (data.get("id") or "").strip()
    if not elevenlabs_tts.find_voice(vid):
        return jsonify({"error": "အဲဒီ အသံ မရှိပါ"}), 404
    ok = elevenlabs_tts.generate_preview(vid)
    resp = {"ok": True, "preview": ok,
            "voices": elevenlabs_tts.get_voices()}
    if not ok:
        resp["note"] = ("preview အသံ ထုတ်လို့ မရပါ — API key နဲ့ "
                        "အင်တာနက် စစ်ပြီး ပြန်စမ်းပါ")
    return jsonify(resp)


@app.delete("/api/settings/elevenlabs-voices")
def elevenlabs_voice_remove():
    data = request.get_json(force=True, silent=True) or {}
    vid = (data.get("id") or "").strip()
    voices = [v for v in elevenlabs_tts.get_voices()
              if v["id"] != vid]
    _env_write("ELEVENLABS_VOICES",
               json.dumps(voices, ensure_ascii=False))
    # Build 207: the removed voice's preview clip goes with it.
    try:
        elevenlabs_tts.preview_path_el(vid).unlink(missing_ok=True)
    except Exception:
        pass
    return jsonify({"ok": True, "voices": voices})


# ---------- Telegram delivery (commercial direction) ----------
# Finished videos can be sent straight to Telegram users from the
# app. Telegram's rule: a bot may only message people who messaged
# it first, so recipients are people who pressed Start on the bot
# (pipeline.telegram_send polls for them). The bot token is set
# here, stored in .env like the Gemini key, never returned.

def _env_write(key, value):
    """Set one .env key via pipeline.envfile (build 192): the
    file is re-read fresh inside an flock, every foreign line
    is preserved byte-for-byte, and the write lands atomically.
    The pre-192 hand-rolled read-modify-write here (and its
    eight inline copies in the key-card handlers) could lose
    OTHER cards' keys when saves overlapped — TG_API_ID /
    TG_API_HASH were lost exactly that way; see
    pipeline/envfile.py."""
    envfile.update(ROOT / ".env", key, value)


def _env_remove(key):
    envfile.update(ROOT / ".env", key, None)


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


# ---------- Local Telegram Bot API server (build 184) ----------
# The cloud Bot API caps bot sends at 50 MB. When the owner saves
# a Telegram api_id + api_hash (from my.telegram.org) here, the
# deploy bootstrap installs a self-hosted telegram-bot-api server
# on this VPS and points the bot at it (TG_API_BASE in .env —
# written by the bootstrap, never from this UI), lifting sends to
# 2 GB so big finished videos land in the chat directly. The hash
# is a credential: it is stored in .env (chmod 600) exactly like
# the bot token, and no endpoint ever returns either value — only
# whether each one is set, plus which API the bot is using now.

def _tg_api_id():
    return _env_value("TG_API_ID")


def _tg_api_hash():
    return _env_value("TG_API_HASH")


@app.get("/api/settings/telegram-api")
def telegram_api_status():
    from pipeline import telegram_send as tg
    return jsonify({"api_id_set": bool(_tg_api_id()),
                    "api_hash_set": bool(_tg_api_hash()),
                    "mode": tg.api_mode()})


@app.post("/api/settings/telegram-api")
def telegram_api_save():
    data = request.get_json(force=True, silent=True) or {}
    api_id = (data.get("api_id") or "").strip()
    api_hash = (data.get("api_hash") or "").strip().lower()
    if not re.fullmatch(r"\d{5,12}", api_id):
        return jsonify({"error": "api_id မမှန်ပါ — my.telegram.org က ရတဲ့ ဂဏန်း သက်သက် ဖြစ်ရပါမယ်"}), 400
    if not re.fullmatch(r"[0-9a-f]{32}", api_hash):
        return jsonify({"error": "api_hash မမှန်ပါ — my.telegram.org က ရတဲ့ စာလုံး ၃၂ လုံး ဖြစ်ရပါမယ်"}), 400
    _env_write("TG_API_ID", api_id)
    _env_write("TG_API_HASH", api_hash)
    from pipeline import telegram_send as tg
    return jsonify({"ok": True, "api_id_set": True,
                    "api_hash_set": True, "mode": tg.api_mode()})


@app.delete("/api/settings/telegram-api")
def telegram_api_delete():
    _env_remove("TG_API_ID")
    _env_remove("TG_API_HASH")
    # Revert to the cloud API at once. The local server (if the
    # bootstrap installed one) is left running but unused; the
    # bootstrap only re-derives TG_API_BASE while credentials
    # exist, so deleting them here is the whole off-switch.
    _env_remove("TG_API_BASE")
    return jsonify({"ok": True, "api_id_set": False,
                    "api_hash_set": False, "mode": "cloud"})


# ---------- VPN tunnel for Gemini/OpenAI (build 189) ----------
# Direct Gemini is location-blocked and direct OpenAI is
# territory-blocked from this VPS — Google/OpenAI judge the
# caller's IP country. The owner pastes a node from his own
# V2Ray subscription here (a vless:// or vmess:// share link, or
# a subscription URL); pipeline/tunnel.py parses it at save time
# into a sing-box client config (tun-singbox.json in the app dir,
# chmod 600; builds 189/190 rendered for xray-core, which refuses
# plain vless nodes — build 191 swapped the engine to sing-box),
# and deploy/activate-settings.sh runs sing-box with it: one
# mixed SOCKS/HTTP proxy inbound on 127.0.0.1, used ONLY by the
# Gemini/OpenAI clients, only after an egress check through the
# proxy has passed (VT_TUNNEL_PROXY + VT_TUNNEL_CONF_SHA in
# .env — written by the bootstrap, never from this UI). The
# pasted link is a credential: it is stored base64'd in .env
# (chmod 600) exactly like the other keys, and no endpoint ever
# returns it or the config — only whether a node is configured,
# its label, whether the tunnel is verified up, the exit
# country, and the engines' last probe verdicts.

@app.get("/api/settings/tunnel")
def tunnel_status():
    from pipeline import tunnel
    info = tunnel.status()
    try:
        probe = json.loads((ROOT / ".translate_probe.json")
                           .read_text(encoding="utf-8"))
        info["gemini_ok"] = probe.get("gemini_ok")
        info["openai_ok"] = probe.get("openai_ok")
        info["probe_ts"] = probe.get("ts")
    except Exception:
        info["gemini_ok"] = None
        info["openai_ok"] = None
        info["probe_ts"] = None
    return jsonify(info)


@app.post("/api/settings/tunnel")
def tunnel_save():
    from pipeline import tunnel
    data = request.get_json(force=True, silent=True) or {}
    raw = (data.get("link") or "").strip()
    if not raw:
        return jsonify({"error": "အရင် link သို့မဟုတ် subscription URL ထည့်ပါ"}), 400
    if len(raw) > 100_000:
        return jsonify({"error": "ထည့်တာ ကြီးလွန်းပါတယ် — link တစ်ခုပဲ ထည့်ပါ"}), 400
    try:
        res = tunnel.save_link(raw)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    info = tunnel.status()
    info.update({"ok": True, **res})
    return jsonify(info)


@app.delete("/api/settings/tunnel")
def tunnel_delete():
    from pipeline import tunnel
    tunnel.clear()
    info = tunnel.status()
    info["ok"] = True
    return jsonify(info)


# ---------- Bilibili login session (build 236) ----------
# Bilibili's risk control refuses this server in every
# anonymous shape tried (direct IP, impersonated ladder,
# Singapore tunnel — builds 233/235); the one credential
# that passes is a logged-in session (the extractor checks
# for a SESSDATA cookie). The owner logs HIS OWN Bilibili
# account in here — by scanning a passport QR with the
# Bilibili app, or by pasting a cookies.txt export — and
# pipeline/download.py hands the stored cookies to yt-dlp
# for bilibili URLs only. Same rules as the tunnel card:
# admin-only (see _accounts_guard — every /api/settings/*
# route needs the admin role), the session lives server-
# side only (bilibili-cookies.txt, chmod 600, never in
# git), and NO endpoint ever returns a cookie value — the
# status surface is {configured, logged_in_as} with the
# account id masked. The QR scan URL is transient (it
# dies with the passport key in ~3 minutes) and is not
# the credential; the cookies are, and they never leave.

@app.get("/api/settings/bilibili")
def bilibili_status():
    from pipeline import bilibili_auth
    return jsonify(bilibili_auth.status())


@app.post("/api/settings/bilibili/qr")
def bilibili_qr_start():
    from pipeline import bilibili_auth
    try:
        return jsonify(bilibili_auth.qr_start())
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


@app.get("/api/settings/bilibili/qr/image")
def bilibili_qr_image():
    from pipeline import bilibili_auth
    try:
        png = bilibili_auth.qr_image_png()
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    resp = Response(png, mimetype="image/png")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/api/settings/bilibili/qr/poll")
def bilibili_qr_poll():
    from pipeline import bilibili_auth
    return jsonify(bilibili_auth.qr_poll())


@app.post("/api/settings/bilibili/cookies")
def bilibili_cookies_save():
    from pipeline import bilibili_auth
    data = request.get_json(force=True, silent=True) or {}
    try:
        res = bilibili_auth.save_pasted(data.get("cookies") or "")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    info = bilibili_auth.status()
    info.update({"ok": True, **res})
    return jsonify(info)


@app.delete("/api/settings/bilibili")
def bilibili_delete():
    from pipeline import bilibili_auth
    bilibili_auth.clear()
    info = bilibili_auth.status()
    info["ok"] = True
    return jsonify(info)


# ---------- Apps Script bridge (server-side only, build 137) ----------
# The owner's AutoTranslate.gs Web App URL + WEB_SECRET. Same rules as
# the Gemini/OpenAI keys: admin-only (see _accounts_guard — every
# /api/settings/* route needs the admin role), stored in the project
# .env (chmod 600, never in git), and no endpoint ever returns the
# secret — only whether a URL + secret are configured. The URL alone
# is also never returned: knowing it without the secret is useless,
# and returning it would leak the owner's script deployment.

@app.get("/api/settings/apps-script")
def apps_script_status():
    from pipeline import apps_script_translate
    return jsonify({"configured": apps_script_translate.configured()})


@app.post("/api/settings/apps-script")
def apps_script_save():
    data = request.get_json(force=True) or {}
    url = (data.get("url") or "").strip()
    secret = (data.get("secret") or "").strip()
    ok_url = url.startswith("https://script.google.com/macros/s/") \
        and url.endswith("/exec") and 40 <= len(url) <= 300
    if not ok_url:
        return jsonify({"error": "URL မမှန်ပါ — Apps Script Web App URL (script.google.com/macros/s/…/exec) ဖြစ်ရပါမယ်"}), 400
    if not (8 <= len(secret) <= 200) or any(ch.isspace() for ch in secret):
        return jsonify({"error": "secret မမှန်ပါ — စာလုံး ၈ လုံး အထက်, space မပါရပါ"}), 400
    _env_write("APPS_SCRIPT_URL", url)
    _env_write("APPS_SCRIPT_SECRET", secret)
    return jsonify({"ok": True, "configured": True})


@app.delete("/api/settings/apps-script")
def apps_script_delete():
    _env_remove("APPS_SCRIPT_URL")
    _env_remove("APPS_SCRIPT_SECRET")
    return jsonify({"ok": True, "configured": False})


def _own_tg_chats(user, job=None):
    """Telegram chats this user may send videos to: ONLY their own
    (build 159 — a finished video must never land in a stranger's
    Telegram). Own = the account's linked chat; a bot-submitted
    job's origin chat; and for the owner, the fixed admin chat."""
    from pipeline import telegram_send as tg
    chats = set()
    if user:
        if user.get("tg_chat_id"):
            chats.add(int(user["tg_chat_id"]))
        if user.get("role") == "admin":
            chats.add(int(tg.FIXED_ADMIN_CHAT_ID))
    if job and job.get("tg_chat_id"):
        chats.add(int(job["tg_chat_id"]))
    return chats


@app.get("/api/telegram/recipients")
def telegram_recipients():
    from pipeline import telegram_send as tg
    user = _current_user()
    if user is None:
        if ACCOUNTS_ENFORCED:
            return jsonify({"error": "login အရင် ဝင်ပါ"}), 401
        return jsonify({"recipients": tg.recipients()})
    allr = tg.recipients()
    own = _own_tg_chats(user)
    out = [r for r in allr if r["chat_id"] in own]
    known = {r["chat_id"] for r in out}
    for cid in sorted(own - known):
        out.append({"chat_id": cid, "username": None,
                    "name": "ကိုယ့် Telegram"})
    return jsonify({"recipients": out})


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


def _auto_recover_lockout():
    """Startup self-heal (build 161): if admin accounts exist but
    EVERY one is disabled — the legacy self-lockout the owner hit —
    re-enable them as the app starts. It can only fire in a total
    lockout: while any admin works, disabled admins stay untouched,
    and build 157 already refuses to disable an admin at all, so a
    disabled admin can only be a leftover from before that guard.
    Deploying code to this server already implies full server
    control, so this grants nothing new — it just ends the lockout
    with zero owner round-trips. Best-effort: never blocks startup."""
    try:
        total, enabled = accounts.admin_counts()
        if total and not enabled:
            for u in accounts.list_admins():
                if u["disabled"]:
                    accounts.set_disabled(u["id"], False)
    except Exception:
        pass


_auto_recover_lockout()


def _stats_reset_once_162():
    """One-time (build 162, owner order): the produced-video counts
    restart from 0 when this build first starts. A marker file in
    the app dir (server-side, survives deploys) makes it one-time;
    the board's own reset button covers future resets."""
    try:
        marker = ROOT / ".stats_reset_162"
        if not marker.exists():
            from pipeline import api_stats
            api_stats.reset()
            marker.write_text("build162", encoding="utf-8")
    except Exception:
        pass


_stats_reset_once_162()


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
    if path.startswith("/api/v1/"):
        # Build 224: the reseller API authenticates with its
        # own Bearer token inside the routes (_api_user) — the
        # web session guard must neither substitute for it nor
        # block it. The two auth paths never mix.
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
    # Behind Caddy the app sees plain HTTP on loopback, so
    # request.is_secure is False even for real HTTPS requests —
    # trust the proxy's X-Forwarded-Proto as well, or the Secure
    # flag never gets set in production (audit C6).
    secure = request.is_secure or \
        request.headers.get("X-Forwarded-Proto", "") == "https"
    resp.set_cookie(_SESSION_COOKIE, token, max_age=30 * 86400,
                    httponly=True, samesite="Lax",
                    secure=secure)
    return resp


_REG_ATTEMPTS = {}
_REG_LOCK = threading.Lock()
_REG_LIMIT = 10
_REG_WINDOW_S = 3600


def _reg_throttled(ip):
    """At most 10 registrations per hour per source — account
    farming is the cheapest abuse there is.

    Build 202 (audit V03): this helper existed but was never
    called; auth_register now checks it FIRST, before any
    password hashing or account creation. The check-and-append
    is atomic under _REG_LOCK (Waitress serves from 8 threads,
    so an unlocked check could be raced past the budget), and
    every access prunes expired hits and drops keys whose last
    hit aged out, so the table cannot grow without bound. The
    state is process-local BY DESIGN: the app runs as a single
    Waitress process, so an in-memory table is the whole
    picture — a multi-process deployment would need a shared
    store instead."""
    now = time.time()
    with _REG_LOCK:
        for key in list(_REG_ATTEMPTS):
            live = [t for t in _REG_ATTEMPTS[key]
                    if now - t < _REG_WINDOW_S]
            if live:
                _REG_ATTEMPTS[key] = live
            else:
                del _REG_ATTEMPTS[key]
        hits = _REG_ATTEMPTS.setdefault(ip, [])
        hits.append(now)
        return len(hits) > _REG_LIMIT


def _reg_retry_after(ip):
    """Seconds until the oldest in-window registration hit from
    this source expires (0 when the source is not throttled) —
    the advisory wait handed back with the 429."""
    now = time.time()
    with _REG_LOCK:
        hits = [t for t in _REG_ATTEMPTS.get(ip, [])
                if now - t < _REG_WINDOW_S]
    if len(hits) <= _REG_LIMIT:
        return 0
    return max(1, int(hits[0] + _REG_WINDOW_S - now) + 1)


@app.post("/api/auth/register")
def auth_register():
    # Build 202 (audit V03): throttle FIRST — before hashing or
    # account work. Password hashing is deliberately expensive
    # (scrypt), so an unthrottled register endpoint is a CPU
    # amplifier as well as a farming hole. Keyed by _client_ip()
    # — the Caddy-appended last X-Forwarded-For entry, the same
    # identity the login lockout trusts — so rotating the
    # client-supplied earlier entries cannot reset the budget.
    ip = _client_ip()
    if _reg_throttled(ip):
        retry = _reg_retry_after(ip)
        resp = jsonify({
            "error": "မှတ်ပုံတင် ကြိုးစားမှု များလွန်းနေပါတယ် — "
                     "ခဏ စောင့်ပြီး ပြန် စမ်းပါ",
            "retry_after": retry})
        if retry:
            resp.headers["Retry-After"] = str(retry)
        return resp, 429
    data = request.get_json(force=True, silent=True) or {}
    # Build 221: optional referral code — validated BEFORE the
    # account exists: an unknown code refuses the signup with a
    # clear message (fix it or clear the field and retry); a
    # known code stamps referred_by on the new account in the
    # same insert. An empty field is a plain signup, unchanged.
    referred_by = None
    ref_code = (data.get("referral_code") or "").strip()
    if ref_code:
        inviter = accounts.user_for_referral_code(ref_code)
        if not inviter:
            return jsonify({"error":
                            "referral code မတွေ့ပါ — ပြန်စစ် "
                            "ထည့်ပါ၊ မရှိရင် ဖြုတ်ပြီး "
                            "ဆက်လုပ်ပါ"}), 400
        referred_by = inviter["id"]
    user, err = accounts.create_user(data.get("username"),
                                     data.get("password"),
                                     referred_by=referred_by)
    if err:
        return jsonify({"error": err}), 400
    return _set_session(jsonify({"user": user}),
                        accounts.new_session(user["id"]))


def _client_ip():
    """The real client IP for throttles/lockouts. Caddy APPENDS the
    client it received the connection from at the END of
    X-Forwarded-For; everything earlier in the header is supplied by
    the client itself — keying the login lockout on the raw header
    let an attacker rotate it away."""
    xff = request.headers.get("X-Forwarded-For") or ""
    if xff:
        return xff.split(",")[-1].strip()
    return request.remote_addr or ""


@app.post("/api/auth/login")
def auth_login():
    data = request.get_json(force=True, silent=True) or {}
    ip = _client_ip()
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


@app.post("/api/auth/google")
def auth_google():
    """Google one-tap sign-in AND account linking (build 180).

    The browser sends the GSI credential (a Google-signed ID
    token); pipeline.google_auth verifies it with Google and this
    route enforces the rest: the email must be Google-verified
    (an unverified email claim is rejected outright — it is the
    only thing tying a token to an identity we act on), a disabled
    account stays out, and the fixed owner email — and ONLY that
    email — promotes its account to admin.

    With a live session this LINKS Google to the signed-in account
    (refused when the sub belongs to someone else); without one it
    signs in the account for the sub, creating it on first use."""
    from pipeline import google_auth
    data = request.get_json(force=True, silent=True) or {}
    token = data.get("credential") or data.get("id_token") or ""
    cid = _google_client_id()
    if not cid:
        return jsonify({"error": "Google login မဖွင့်ရသေးပါ"}), 400
    info = google_auth.verify_id_token(token, cid)
    if not info:
        return jsonify({"error": "Google အတည်ပြုချက် မအောင်မြင်ပါ — ပြန် ကြိုးစားပေးပါ"}), 401
    if not info["email_verified"]:
        return jsonify({"error": "ဒီ Google account ရဲ့ email ကို Google က အတည်မပြုရသေးပါ"}), 401
    me = _current_user()
    if me:
        if not accounts.link_google(me["id"], info["sub"], info["email"]):
            return jsonify({"error": "ဒီ Google account က အခြား account နဲ့ ချိတ်ပြီးသား ဖြစ်နေပါတယ်"}), 409
        user = accounts.get_user(me["id"])
    else:
        user = accounts.user_for_google_sub(info["sub"])
        if user is None:
            user = accounts.create_google_user(info["sub"], info["email"])
        if user["disabled"]:
            return jsonify({"error": "ဒီ account ပိတ်ထားပါတယ် — admin ကို ဆက်သွယ်ပါ"}), 401
    # Fixed owner email (amendment): a verified token for exactly
    # this address makes its account an admin — the ONLY way the
    # Google path can produce or confirm one. Existing accounts
    # and flags are otherwise left exactly as they are.
    if info["email"] == FIXED_ADMIN_EMAIL and user["role"] != "admin":
        accounts.ensure_admin(user["id"])
        user = accounts.get_user(user["id"])
    accounts.touch_login(user["id"])
    if me:
        return jsonify({"user": user})
    return _set_session(jsonify({"user": user}),
                        accounts.new_session(user["id"]))


_PROBE = {"running": False}
_PROBE_LOCK = threading.Lock()
_BOOT_TS = time.time()


def _run_translate_probe():
    """Live translation self-test (one short Chinese sentence):
    can the LLM engines produce Myanmar right now? Result
    cached to .translate_probe.json for /api/auth/me. Build
    231: the free-chain legs are gone with the chain itself —
    the probe now covers the LLM routes only."""
    try:
        from pipeline import gemini_translate
        res = {"ts": time.time(), "gemini_ok": None}
        sample = "你好，这是一个测试。"

        try:
            if gemini_translate.key_configured():
                by = gemini_translate.translate_all(
                    [{"start": 0.0, "end": 1.0, "text": sample}])
                out = by.get(0) or ""
                res["gemini_ok"] = bool(out) and \
                    any("\u1000" <= c <= "\u109f" for c in out)
            else:
                res["gemini_ok"] = False
        except Exception as e:
            res["gemini_ok"] = False
            # Build 197: the Gemini walk records a body snippet
            # per attempt; 500 chars cut off before the later
            # models, so the cap is raised modestly to keep the
            # discriminating attempts visible.
            res["gemini_err"] = str(e)[:1200]
        if not res.get("gemini_ok"):
            # Build 164: when generateContent 404s everywhere, ask
            # the project what it CAN see — the model catalogue is
            # the ground truth for the next fix.
            try:
                names, lerr = gemini_translate.list_models()
                res["gemini_models"] = names[:25]
                if lerr:
                    res["gemini_models_err"] = lerr[:200]
            except Exception as e:
                res["gemini_models_err"] = str(e)[:200]
        try:
            from pipeline import openai_translate
            if openai_translate.key_configured():
                by = openai_translate.translate_all(
                    [{"start": 0.0, "end": 1.0, "text": sample}])
                out = by.get(0) or ""
                res["openai_ok"] = bool(out) and \
                    any("\u1000" <= c <= "\u109f" for c in out)
            else:
                res["openai_ok"] = False
        except Exception as e:
            res["openai_ok"] = False
            res["openai_err"] = str(e)[:200]
        try:
            from pipeline import openrouter_translate
            if openrouter_translate.key_configured():
                by = openrouter_translate.translate_all(
                    [{"start": 0.0, "end": 1.0, "text": sample}])
                out = by.get(0) or ""
                res["openrouter_ok"] = bool(out) and \
                    any("\u1000" <= c <= "\u109f" for c in out)
            else:
                res["openrouter_ok"] = False
        except Exception as e:
            res["openrouter_ok"] = False
            res["openrouter_err"] = str(e)[:400]
        try:
            res["openrouter_model"] = (
                (ROOT / ".openrouter_last_model").read_text(
                    encoding="utf-8").strip() or None)
        except Exception:
            res["openrouter_model"] = None
        try:
            from pipeline import xkiro_translate
            if xkiro_translate.key_configured():
                by = xkiro_translate.translate_all(
                    [{"start": 0.0, "end": 1.0, "text": sample}])
                out = by.get(0) or ""
                res["xkiro_ok"] = bool(out) and \
                    any("\u1000" <= c <= "\u109f" for c in out)
            else:
                res["xkiro_ok"] = False
        except Exception as e:
            res["xkiro_ok"] = False
            res["xkiro_err"] = str(e)[:400]
        try:
            res["xkiro_model"] = (
                (ROOT / ".xkiro_last_model").read_text(
                    encoding="utf-8").strip() or None)
        except Exception:
            res["xkiro_model"] = None
        try:
            from pipeline import apps_script_translate
            if apps_script_translate.configured():
                by = apps_script_translate.translate_all(
                    [{"start": 0.0, "end": 1.0, "text": sample}])
                out = by.get(0) or ""
                res["apps_script_ok"] = bool(out) and \
                    any("\u1000" <= c <= "\u109f" for c in out)
            else:
                res["apps_script_ok"] = False
        except Exception as e:
            res["apps_script_ok"] = False
            res["apps_script_err"] = str(e)[:200]
        (ROOT / ".translate_probe.json").write_text(
            json.dumps(res), encoding="utf-8")
    except Exception:
        pass
    finally:
        _PROBE["running"] = False


def _translate_probe_maybe():
    pf = ROOT / ".translate_probe.json"
    data = None
    try:
        data = json.loads(pf.read_text(encoding="utf-8"))
    except Exception:
        data = None
    # Stale when older than 6 h OR older than this process: a
    # fresh deploy must re-probe at once, not serve the previous
    # build's verdict (build 126's first check hit exactly that).
    # Build 171: the TTL was 30 min — every re-probe burns real
    # translation calls from the SAME free-tier quota that real
    # jobs need (the OpenRouter free endpoints allow only ~50
    # requests a day), so the health board now refreshes at most
    # every 6 h instead of starving jobs for a fresher hint.
    if not data or float(data.get("ts") or 0) < _BOOT_TS \
            or time.time() - float(data.get("ts") or 0) > 21600:
        with _PROBE_LOCK:
            if not _PROBE["running"]:
                _PROBE["running"] = True
                threading.Thread(target=_run_translate_probe,
                                 daemon=True).start()
    return data


@app.get("/api/auth/me")
def auth_me():
    claim_open = False
    try:
        total_admins, enabled_admins = accounts.admin_counts()
        if total_admins == 0:
            claim_open = True
        elif enabled_admins == 0:
            # Lockout recovery (build 157): every admin is disabled —
            # the claim button shows ONLY for the account linked to
            # the owner's fixed Telegram chat.
            me = _current_user()
            try:
                from pipeline import telegram_send as _tgmod
                fixed = _tgmod.FIXED_ADMIN_CHAT_ID
            except Exception:
                fixed = None
            claim_open = bool(me and fixed and
                              str(me.get("tg_chat_id")) == str(fixed))
    except Exception:
        claim_open = True
    fonts = {}
    try:
        from pipeline import burn as _burn
        from pipeline.common import FONTS_DIR as _FD
        fonts = {fam: ((_FD / fn).exists()
                       and (_FD / fn).stat().st_size > 10_000)
                 for fam, fn in _burn._FONT_FILES.items()}
    except Exception:
        fonts = {}
    i18n = {"probe": _translate_probe_maybe()}
    try:
        from pipeline import gemini_translate as _gt
        i18n["gemini_configured"] = bool(_gt.key_configured())
    except Exception:
        i18n["gemini_configured"] = None
    try:
        from pipeline import openai_translate as _oa
        i18n["openai_configured"] = bool(_oa.key_configured())
    except Exception:
        i18n["openai_configured"] = None
    try:
        from pipeline import openrouter_translate as _or
        i18n["openrouter_configured"] = bool(_or.key_configured())
    except Exception:
        i18n["openrouter_configured"] = None
    try:
        from pipeline import xkiro_translate as _xk
        i18n["xkiro_configured"] = bool(_xk.key_configured())
    except Exception:
        i18n["xkiro_configured"] = None
    try:
        from pipeline import apps_script_translate as _as
        i18n["apps_script_configured"] = bool(_as.configured())
    except Exception:
        i18n["apps_script_configured"] = None
    try:
        i18n["last_provider"] = (
            (ROOT / ".translate_good").read_text(
                encoding="utf-8").strip() or None)
    except Exception:
        i18n["last_provider"] = None
    return jsonify({"user": _current_user(),
                    "admin_claim_open": claim_open,
                    "enforced": ACCOUNTS_ENFORCED,
                    "google_client_id": _google_client_id() or None,
                    "fonts": fonts,
                    "i18n": i18n})


@app.post("/api/auth/change-password")
def auth_change_password():
    user = _current_user()
    if not user:
        return jsonify({"error": "အရင် login ဝင်ပါ"}), 401
    data = request.get_json(force=True, silent=True) or {}
    ok = accounts.set_password(user["id"], data.get("password"))
    if not ok:
        return jsonify({"error": "password အနည်းဆုံး ၆ လုံး ထားပါ"}), 400
    # set_password revoked every session (including this one);
    # mint a fresh one so THIS device stays signed in.
    resp = jsonify({"ok": True})
    return _set_session(resp, accounts.new_session(user["id"]))


@app.get("/api/referral")
def referral_info():
    """Build 221: the signed-in account's referral block — own
    code, inviter (if any), referral count, credits earned, and
    the current reward size so the profile can say exactly what
    an invite pays."""
    user = _current_user()
    if not user:
        return jsonify({"error": "အရင် login ဝင်ပါ"}), 401
    stats = accounts.referral_stats(user["id"])
    if stats is None:
        return jsonify({"error": "account မတွေ့ပါ"}), 404
    stats["reward"] = _referral_reward_credits()
    return jsonify(stats)


@app.post("/api/referral/apply")
def referral_apply():
    """Build 221: set-once inviter entry from the profile (the
    path Google-born accounts use — they never saw the register
    form). Own code refused; unknown code refused; once set it
    can never be changed."""
    user = _current_user()
    if not user:
        return jsonify({"error": "အရင် login ဝင်ပါ"}), 401
    data = request.get_json(force=True, silent=True) or {}
    code = (data.get("code") or "").strip()
    if not code:
        return jsonify({"error": "code အရင် ထည့်ပါ"}), 400
    inviter = accounts.user_for_referral_code(code)
    if not inviter:
        return jsonify({"error": "referral code မတွေ့ပါ"}), 400
    if inviter["id"] == user["id"]:
        return jsonify({"error":
                        "ကိုယ့် code ကိုယ် ပြန် ထည့်လို့ "
                        "မရပါ"}), 400
    if not accounts.set_referred_by(user["id"], inviter["id"]):
        return jsonify({"error":
                        "referral code သတ်မှတ်ပြီးသား "
                        "ဖြစ်နေပါတယ် — ပြန် ပြောင်းလို့ "
                        "မရပါ"}), 400
    stats = accounts.referral_stats(user["id"])
    stats["reward"] = _referral_reward_credits()
    return jsonify({"ok": True, **stats})


@app.post("/api/auth/setup")
def auth_setup():
    """Build 187: complete the one-time username+password setup
    for a Google-born account. Idempotent by design — an account
    that never needed setup, or already finished it, gets a clean
    already-response and can NOT be renamed through here. The
    session is deliberately untouched: completing setup must not
    log the user out of the device they are setting up on."""
    user = _current_user()
    if not user:
        return jsonify({"error": "အရင် login ဝင်ပါ"}), 401
    data = request.get_json(force=True, silent=True) or {}
    res = accounts.complete_setup(user["id"], data.get("username"),
                                  data.get("password"))
    if res["error"]:
        return jsonify({"error": res["error"]}), 400
    if res["already"]:
        return jsonify({"user": res["user"], "already": True})
    return jsonify({"user": res["user"]})


@app.post("/api/admin/claim")
def admin_claim():
    """One-shot bootstrap: the signed-in account becomes admin, but
    only while no admin exists yet (behind the owner's wall)."""
    user = _current_user()
    if not user:
        return jsonify({"error": "အရင် login ဝင်ပါ"}), 401
    fixed = None
    try:
        from pipeline import telegram_send as _tg
        fixed = _tg.FIXED_ADMIN_CHAT_ID
    except Exception:
        pass
    if not accounts.claim_admin(user["username"], recovery_tg_id=fixed):
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


@app.get("/api/admin/api-stats")
def admin_api_stats():
    """Which translation API produced how many videos (build 138).
    Counts live in pipeline/api_stats.py (server-side JSON)."""
    if not _require_admin():
        return jsonify({"error": "admin ပဲ ရပါတယ်"}), 403
    from pipeline import api_stats
    return jsonify(api_stats.read())


@app.post("/api/admin/api-stats/reset")
def admin_api_stats_reset():
    """Zero the usage board (build 162). Admin-only, like the read."""
    if not _require_admin():
        return jsonify({"error": "admin ပဲ ရပါတယ်"}), 403
    from pipeline import api_stats
    return jsonify(api_stats.reset())


@app.get("/api/admin/timing-stats")
def admin_timing_stats():
    """Per-stage average seconds over the last 50 finished
    jobs (build 213) — the owner's hardware-decision board:
    which stage eats the time, on real production jobs.
    Admin-only, like the usage board above. Each stage is
    averaged over the jobs that HAVE that stage (a subs job
    has no tts; a studio job has only tts), and counts ride
    along so a thin average is visible as thin."""
    if not _require_admin():
        return jsonify({"error": "admin ပဲ ရပါတယ်"}), 403
    keys = ("download", "transcribe", "translate", "script",
            "tts", "render", "queue_s", "total_s")
    fin = [j for j in _all_jobs()
           if j.get("status") in ("done", "error")
           and j.get("finished_at")]
    fin.sort(key=lambda j: j.get("finished_at") or 0,
             reverse=True)
    fin = fin[:50]
    sums, counts = {}, {}
    for j in fin:
        t = _timings_of(j)
        for k in keys:
            v = t.get(k)
            if isinstance(v, (int, float)):
                sums[k] = sums.get(k, 0.0) + float(v)
                counts[k] = counts.get(k, 0) + 1
    avg = {k: round(sums[k] / counts[k], 1) for k in counts}
    return jsonify({"jobs": len(fin), "avg": avg,
                    "counts": counts})


@app.get("/api/admin/golden-check")
def admin_golden_check():
    """Golden Sample regression status + last report (build
    230) — the fixed reference-clip set that catches silent
    engine/TTS quality drift before customers do. Admin-only,
    like the boards above; state lives in
    pipeline/golden_check.py."""
    if not _require_admin():
        return jsonify({"error": "admin ပဲ ရပါတယ်"}), 403
    from pipeline import golden_check
    return jsonify(golden_check.status())


@app.post("/api/admin/golden-check")
def admin_golden_check_run():
    """Start a Golden Sample regression run in the background
    (build 230). A full run takes minutes (Whisper + translate
    over 5 clips), so the request only starts it — poll the
    GET for the report."""
    if not _require_admin():
        return jsonify({"error": "admin ပဲ ရပါတယ်"}), 403
    from pipeline import golden_check
    if not golden_check.start("check"):
        return jsonify({"error": "စစ်ဆေးမှု လုပ်နေဆဲ ဖြစ်ပါတယ်"}), 409
    return jsonify({"ok": True, "started": True})


@app.post("/api/admin/golden-check/capture")
def admin_golden_check_capture():
    """Re-capture the Golden baselines from CURRENT code and
    engines (build 230) — once on a new server so its baselines
    match its engines, and after any intentional engine or
    prompt change."""
    if not _require_admin():
        return jsonify({"error": "admin ပဲ ရပါတယ်"}), 403
    from pipeline import golden_check
    if not golden_check.start("capture"):
        return jsonify({"error": "စစ်ဆေးမှု လုပ်နေဆဲ ဖြစ်ပါတယ်"}), 409
    return jsonify({"ok": True, "started": True})


@app.post("/api/topup")
def topup_create():
    """Customer submits a credit top-up from the WEB app (build 156):
    amount + transfer screenshot. Mirrors the Telegram bot flow — a
    pending topups row + proof file under topups/<id>.jpg; the admin
    decides from the web panel or the Telegram approval card, and
    decide_topup stays exactly-once across both surfaces."""
    user = _current_user()
    if not user:
        return jsonify({"error": "အရင် login ဝင်ပါ"}), 401
    # Build 231: a package pick derives the credited amount
    # from the PACKAGES table (never from the client), so what
    # approval credits is exactly base+bonus.
    package_kyat = None
    pkg_raw = (request.form.get("package") or "").strip()
    if pkg_raw:
        try:
            package_kyat = int(pkg_raw)
            assert package_kyat in PACKAGES
        except Exception:
            return jsonify({"error": "package မမှန်ပါ"}), 400
        amount = PACKAGES[package_kyat][0]
    else:
        try:
            amount = int(request.form.get("amount") or "")
            assert 1 <= amount <= 100000
        except Exception:
            return jsonify({"error": "credit အရေအတွက် မှန်အောင် "
                                     "ထည့်ပါ (ဥပမာ 1000)"}), 400
    f = request.files.get("proof")
    if not f or not f.filename:
        return jsonify({"error": "ငွေလွှဲ screenshot ထည့်ပါ"}), 400
    tdir = ROOT / "topups"
    tdir.mkdir(exist_ok=True)
    tag = uuid.uuid4().hex[:12]
    raw = tdir / f".tmp-{tag}.bin"
    tmp = tdir / f".tmp-{tag}.jpg"
    f.save(raw)
    try:
        if raw.stat().st_size > 10 * 1024 * 1024:
            raise ValueError("too big")
        from PIL import Image
        with Image.open(raw) as im:
            im.verify()
        with Image.open(raw) as im:
            im.convert("RGB").save(tmp, "JPEG", quality=88)
    except Exception:
        raw.unlink(missing_ok=True)
        tmp.unlink(missing_ok=True)
        return jsonify({"error": "ပုံ မဟုတ်ပါ (သို့) 10MB ထက် ကြီးနေပါတယ် — "
                                 "screenshot အစစ် ထည့်ပါ"}), 400
    raw.unlink(missing_ok=True)
    tid = accounts.create_topup(user["id"], amount, None,
                                package_kyat=package_kyat)
    proof = tdir / f"{tid}.jpg"
    tmp.replace(proof)
    accounts.set_topup_proof(tid, proof.name)
    try:
        from pipeline import telegram_send as tg
        cap = (f"💰 Credit ဖြည့် တောင်းဆိုချက် #{tid}\n"
               f"👤 {user['username']} · {amount} credits (web app)")
        if package_kyat:
            cap += (f"\n📦 Package {package_kyat:,} ကျပ် "
                    f"(bonus +{PACKAGES[package_kyat][1]})")
        tg.send_photo(tg.FIXED_ADMIN_CHAT_ID, proof, caption=cap,
                      buttons=[[("✅ Approve", f"t:{tid}:ok"),
                                ("❌ ငြင်း", f"t:{tid}:no")]])
    except Exception:
        pass
    return jsonify({"ok": True, "topup_id": tid, "status": "pending"})


@app.get("/api/topup")
def topup_mine():
    """The logged-in customer's own recent top-up requests."""
    user = _current_user()
    if not user:
        return jsonify({"error": "အရင် login ဝင်ပါ"}), 401
    return jsonify({"topups": accounts.topups_for(user["id"])})


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
        return jsonify({"error": "ဆုံးဖြတ်ပြီးသား / မတွေ့ / screenshot "
                                 "မရှိလို့ approve မရပါ"}), 409
    if res.get("tg_chat_id"):
        try:
            from pipeline import telegram_send as tg
            tg.send_message(
                res["tg_chat_id"],
                f"💳 Credit {res['credits']} ဝင်ပါပြီ — ကျေးဇူးတင်ပါတယ် 🙏"
                if data.get("decision") == "approved" else
                "🙏 တောင်းပန်ပါတယ် — Credit ဖြည့် တောင်းဆိုချက် ကို လက်ခံလို့ မရပါ ဖြစ်သွားပါတယ်။ လွှဲပြီး screenshot ကို စစ်ပြီး ပြန် ပို့ကြည့်ပေးပါ")
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


# ---------- Google sign-in settings (build 180) ----------
# The client ID is configured by the admin here and stored in .env
# like the API keys; this settings endpoint only ever reports
# WHETHER one is set, never the value (same rule as the keys). The
# browser still needs the id itself to render the Google button —
# a client ID is a public identifier by design (it ships in every
# page that offers Google sign-in), so /api/auth/me exposes just
# the id for the login view. There is no client SECRET in this
# flow at all, so nothing secret exists to leak.

@app.get("/api/settings/google")
def google_settings_status():
    return jsonify({"configured": bool(_google_client_id())})


@app.post("/api/settings/google")
def google_settings_save():
    data = request.get_json(force=True, silent=True) or {}
    cid = (data.get("client_id") or "").strip()
    ok_format = cid.endswith(".apps.googleusercontent.com") \
        and 20 <= len(cid) <= 120 \
        and all(ch.isalnum() or ch in "-." for ch in cid)
    if not ok_format:
        return jsonify({"error": "Client ID မမှန်ပါ — …apps.googleusercontent.com နဲ့ ဆုံးတဲ့ Google Client ID ဖြစ်ရပါမယ်"}), 400
    _env_write("GOOGLE_CLIENT_ID", cid)
    return jsonify({"ok": True, "configured": True})


@app.delete("/api/settings/google")
def google_settings_delete():
    _env_remove("GOOGLE_CLIENT_ID")
    return jsonify({"ok": True, "configured": False})


# ---------- daily job limit settings (build 180) ----------

@app.get("/api/settings/daily-limit")
def daily_limit_status():
    return jsonify({"limit": _daily_job_limit()})


@app.post("/api/settings/daily-limit")
def daily_limit_save():
    data = request.get_json(force=True, silent=True) or {}
    try:
        limit = int(data.get("limit"))
    except (TypeError, ValueError):
        return jsonify({"error": "ကိန်းဂဏန်း ထည့်ပါ (0 = အကန့်အသတ် မရှိ)"}), 400
    limit = max(0, min(10000, limit))
    _env_write("DAILY_JOB_LIMIT", str(limit))
    return jsonify({"ok": True, "limit": limit})


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
        if not accounts.set_disabled(uid, bool(data["disabled"])):
            return jsonify({"error": "admin account ကို ပိတ်လို့ မရပါ — "
                                     "admin ပျောက်သွားနိုင်ပါတယ်"}), 400
    if data.get("credits_delta"):
        if accounts.adjust_credits(uid, int(data["credits_delta"])) is None:
            return jsonify({"error": "credit မလုံလောက်ပါ — အဲဒီလောက် လျှော့လို့ မရပါ"}), 400
    if data.get("new_password"):
        if not accounts.set_password(uid, data["new_password"]):
            return jsonify({"error": "password အနည်းဆုံး ၆ လုံး ထားပါ"}), 400
    if data.get("tg_chat_id"):
        if not accounts.link_tg(uid, int(data["tg_chat_id"])):
            return jsonify({"error": "အဲ့ဒီ Telegram က အခြား account နဲ့ "
                                     "ချိတ်ထားပြီးသား ဖြစ်နေပါတယ်"}), 400
    return jsonify({"user": accounts.get_user(uid)})


def _tg_worker(job, chat_id, out_path):
    from pipeline import telegram_send as tg
    try:
        caption = "🎬 မြန်မာ ဘာသာပြန် ဗီဒီယို\n" + (job.get("url") or "") + "\n🙏 ကျေးဇူးတင်ပါတယ်"
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
    if not job or not _owns(_current_user(), job):
        return jsonify({"error": "job မတွေ့ပါ"}), 404
    out = JOBS_DIR / jid / "output.mp4"
    if not out.exists():
        return jsonify({"error": "ဗီဒီယို မပြီးသေးပါ"}), 400
    data = request.get_json(force=True) or {}
    user = _current_user()
    if user is None and not ACCOUNTS_ENFORCED:
        allowed = {r["chat_id"] for r in tg.recipients()}
    else:
        allowed = _own_tg_chats(user, job)
    try:
        chat_id = int(data.get("chat_id"))
    except (TypeError, ValueError):
        chat_id = None
    if chat_id is None or chat_id not in allowed:
        return jsonify({"error": "ကိုယ့် Telegram ကိုပဲ ပို့လို့ရပါတယ် — "
                                 "သူများ Telegram ကို ပို့ခွင့် မရှိပါ"}), 403
    size = out.stat().st_size
    if size > tg.send_cap():
        cap_mb = tg.send_cap() // 1_000_000
        return jsonify({"error": f"ဖိုင် {size / 1e6:.0f}MB ရှိနေတယ် — Telegram bot က {cap_mb}MB အထိပဲ ပို့လို့ရပါတယ်"}), 400
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
    (burn.SUBS_DEFAULTS: size 100, width 95, bottom 10, bottom-aligned).
    size_pct clamps to 10–120 since build 172 (owner order 2026-11-13),
    matching the wizard slider and burn._SUBS_RANGES."""
    s = data.get("subs") or {}
    return {
        "size_pct": _clamp_num(s.get("size_pct"), 10, 120, 100),
        "width_pct": _clamp_num(s.get("width_pct"), 50, 100, 95),
        "bottom_pct": _clamp_num(s.get("bottom_pct"), 2, 25, 10),
        "align": "top" if s.get("align") == "top" else "bottom",
        "color": s.get("color") if s.get("color") in
                 ("white", "yellow", "blue", "green", "red", "black")
                 else "white",
        "placement": s.get("placement") if s.get("placement") in
                     ("box", "bottom", "top") else "box",
    }


def _resolve_el_entry(user, voice_id):
    """Resolve an ElevenLabs "el:" pick for THIS caller ->
    (entry, gate_blocked). The ONE resolution every job surface
    (dub / studio / recap / bot) shares, so the rules can never
    drift between them:

    - A REGISTRY voice resolves for everyone, but the build-206
      gate may still block the caller (gate_blocked=True) —
      exactly the pre-212 rule.
    - A voice that is the CALLER'S OWN clone (build 212)
      resolves with gate_blocked=False: ownership is the
      permission, the test-mode gate is about the catalogue.
    - Anything else — a removed registry voice, or a clone
      belonging to someone else — resolves to (None, False);
      callers answer with the removed-voice message, so a
      stranger's clone is indistinguishable from a voice that
      does not exist."""
    entry = elevenlabs_tts.find_voice(voice_id)
    if entry is not None:
        blocked = user is not None \
            and not elevenlabs_tts.elevenlabs_allowed(user)
        return entry, blocked
    if user is not None and user.get("id"):
        clone = accounts.clone_by_voice(voice_id)
        if clone and clone["user_id"] == user["id"]:
            return {"id": str(voice_id), "name": clone["name"],
                    "clone": True}, False
    return None, False


# ---------- chunked upload (build 242) ----------
# A single POST of a very large file can be refused by an
# intermediary the app does not control: on 2026-11-21 the
# owner's ~865 MB upload was answered HTTP 413 by a proxy
# on HIS network path, while the server provably accepted
# a 996 MB POST from a clean network (no MAX_CONTENT_LENGTH
# in the app, no body limit in the Caddy block; the app's
# own ceiling is the download chain's 2 GiB, with its own
# Burmese error). Per-request size caps on consumer ISP
# proxies are outside our control, so large files now
# travel in slices: start declares the file (extension
# whitelist + the same 2 GiB ceiling), chunk posts land
# at their index offsets (a retried chunk rewrites the
# same bytes — idempotent), finish verifies the assembly
# exactly (every chunk present at its exact length, the
# total equal to the declaration) plus the same ffprobe
# readability check the direct path runs, and hands back
# an unguessable upload_token. The file-consuming routes
# then accept the token INSTEAD of the file part and
# proceed exactly as if the file had been posted: a job
# spends the token (the staged file is dropped once
# _accept_upload has copied it into the job dir), while
# frame-upload only reads it in place and never spends
# it. Records are memory-only, bound to the account that
# started them; a sweeper reaps anything untouched for
# _UPLOAD_TTL, and staging dirs orphaned by a restart are
# reaped by mtime.
CHUNKED_DIR = UPLOADS_DIR / "chunked"
CHUNKED_DIR.mkdir(exist_ok=True)
_UPLOAD_TTL = 2 * 3600
_UPLOAD_CHUNK_MAX = 16 * 1024 * 1024
_UPLOAD_CHUNK_DEFAULT = 8 * 1024 * 1024
_UPLOAD_CHUNK_MIN = 64 * 1024
_UPLOADS = {}        # upload_id -> record
_UPLOAD_TOKENS = {}  # upload_token -> upload_id
_UPLOADS_LOCK = threading.Lock()
_UPLOAD_ADMIT = {}   # admission key -> [post timestamps]


def _upload_admit(kind):
    """Per-user admission for the chunked-upload posts, in
    the _preview_allowed shape: starts are capped like
    previews (30/hour); chunk posts get a far higher cap
    (a 2 GiB file at the 8 MiB default is 256 posts, and
    two concurrent uploads plus retries must fit) — the
    hard bounds stay the per-chunk ceiling, the 2 GiB
    total, and the concurrent-upload cap in upload_start."""
    me = _current_user()
    key = f"u{me['id']}" if me else f"ip:{_client_ip()}"
    cap = 30 if kind == "start" else 2000
    now = time.time()
    hits = [t for t in _UPLOAD_ADMIT.get(key, [])
            if now - t < 3600]
    if len(hits) >= cap:
        _UPLOAD_ADMIT[key] = hits
        return False
    hits.append(now)
    _UPLOAD_ADMIT[key] = hits
    return True


def _upload_owner_ok(rec, user):
    """A record belongs to the account that started it.
    With accounts off there is no caller identity (user
    is None) and the unguessable id/token is itself the
    capability, as everywhere else in this app."""
    if user is None:
        return True
    return rec.get("owner") == user.get("id")


def _upload_for_token(token, user):
    """Resolve a finished upload_token to its record for
    consumption, or None (unknown, unfinished, not the
    caller's, or the staged file already gone)."""
    if not token:
        return None
    with _UPLOADS_LOCK:
        uid = _UPLOAD_TOKENS.get(token)
        rec = _UPLOADS.get(uid) if uid else None
    if rec is None or rec.get("state") != "ready":
        return None
    if not _upload_owner_ok(rec, user):
        return None
    if not Path(rec["path"]).exists():
        return None
    return rec


def _drop_upload(rec):
    with _UPLOADS_LOCK:
        _UPLOADS.pop(rec["id"], None)
        if rec.get("token"):
            _UPLOAD_TOKENS.pop(rec["token"], None)
    shutil.rmtree(rec["dir"], ignore_errors=True)


def _spend_upload(rec):
    """A job consumed the token: _accept_upload has already
    copied the bytes into the job dir, so the staged file
    and the record go away — the token can never be
    spent twice."""
    _drop_upload(rec)


def _expire_uploads():
    now = time.time()
    with _UPLOADS_LOCK:
        dead = [r for r in _UPLOADS.values()
                if now - r.get("touched", 0) > _UPLOAD_TTL]
        for r in dead:
            _UPLOADS.pop(r["id"], None)
            if r.get("token"):
                _UPLOAD_TOKENS.pop(r["token"], None)
    for r in dead:
        shutil.rmtree(r["dir"], ignore_errors=True)


def _upload_sweeper():
    """Reap expired uploads, plus staging dirs orphaned by
    a restart (records are memory-only, so a restart
    orphans every staged file; mtime is the only clock
    they have left)."""
    time.sleep(120)
    while True:
        try:
            _expire_uploads()
        except Exception:
            pass
        try:
            live = set(_UPLOADS)
            for d in list(CHUNKED_DIR.iterdir()):
                try:
                    if d.is_dir() and d.name not in live \
                            and time.time() - d.stat().st_mtime \
                            > _UPLOAD_TTL:
                        shutil.rmtree(d, ignore_errors=True)
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(600)


@app.post("/api/upload/start")
def upload_start():
    """Declare a chunked upload: {filename, size,
    chunk_size?} -> {upload_id}. Session-authenticated by
    the accounts guard like every /api route; the record
    is bound to the caller and at most two uploads may be
    open (still receiving) per account at once."""
    if not _upload_admit("start"):
        return jsonify({"error": "ခဏ ခဏ မစမ်းပါနဲ့ — ခဏ စောင့်ပြီး "
                                 "ပြန် စမ်းပါ"}), 429
    me = _current_user()
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("filename") or "").strip()
    ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in UPLOAD_EXTS:
        return jsonify({"error": "ဒီ ဖိုင် အမျိုးအစား မရပါ — mp4, mkv, "
                                 "mov, avi, webm ဖိုင် ပဲ ရပါတယ်"}), 400
    try:
        size = int(data.get("size"))
    except (TypeError, ValueError):
        size = 0
    if size <= 0:
        return jsonify({"error": "ဖိုင် အရွယ် မမှန်ပါ"}), 400
    if size > dl_mod.MAX_DOWNLOAD_BYTES:
        return jsonify({"error": "ဖိုင် အရွယ် ကြီးလွန်းပါတယ် — "
                                 "အများဆုံး 2 GB ပဲ ရပါတယ်"}), 400
    try:
        chunk_size = int(data.get("chunk_size")
                         or _UPLOAD_CHUNK_DEFAULT)
    except (TypeError, ValueError):
        chunk_size = 0
    if not (_UPLOAD_CHUNK_MIN <= chunk_size
            <= _UPLOAD_CHUNK_MAX):
        return jsonify({"error": "chunk အရွယ် မမှန်ပါ"}), 400
    owner = me["id"] if me else None
    with _UPLOADS_LOCK:
        open_n = sum(1 for r in _UPLOADS.values()
                     if r.get("owner") == owner
                     and r.get("state") == "open")
        if open_n >= 2:
            return jsonify({"error": "upload နှစ်ခု တပြိုင်တည်း "
                                     "တင်နေပါတယ် — တစ်ခု ပြီးမှ "
                                     "နောက်တစ်ခု စပါ"}), 429
        uid = uuid.uuid4().hex
        sdir = CHUNKED_DIR / uid
        sdir.mkdir(parents=True, exist_ok=True)
        path = sdir / ("data" + ext)
        path.touch()
        _UPLOADS[uid] = {
            "id": uid, "owner": owner, "name": name[:120],
            "ext": ext, "size": size, "chunk_size": chunk_size,
            "dir": sdir, "path": path, "state": "open",
            "lens": {}, "created": time.time(),
            "touched": time.time(), "token": None,
        }
    return jsonify({"upload_id": uid})


@app.post("/api/upload/chunk")
def upload_chunk():
    """One slice: multipart {upload_id, index, chunk}. The
    bytes land at index * chunk_size, so a retried chunk
    rewrites the same bytes; per-chunk ceiling 16 MB, and
    a chunk may never reach past the declared total."""
    if not _upload_admit("chunk"):
        return jsonify({"error": "ခဏ ခဏ မစမ်းပါနဲ့ — ခဏ စောင့်ပြီး "
                                 "ပြန် စမ်းပါ"}), 429
    me = _current_user()
    uid = (request.form.get("upload_id") or "").strip()
    try:
        index = int(request.form.get("index"))
    except (TypeError, ValueError):
        index = -1
    f = request.files.get("chunk")
    with _UPLOADS_LOCK:
        rec = _UPLOADS.get(uid)
    if rec is None or rec.get("state") != "open":
        return jsonify({"error": "upload မတွေ့ပါ — အစက ပြန် "
                                 "တင်ပါ"}), 400
    if not _upload_owner_ok(rec, me):
        return jsonify({"error": "ဒီ upload က သင့်ဟာ "
                                 "မဟုတ်ပါ"}), 403
    n = -(-rec["size"] // rec["chunk_size"])
    if index < 0 or index >= n or f is None:
        return jsonify({"error": "chunk မမှန်ပါ"}), 400
    data = f.stream.read(_UPLOAD_CHUNK_MAX + 1)
    if len(data) > _UPLOAD_CHUNK_MAX:
        return jsonify({"error": "chunk ကြီးလွန်းပါတယ် — အများဆုံး "
                                 "16 MB ပဲ ရပါတယ်"}), 400
    if len(data) > rec["chunk_size"]:
        return jsonify({"error": "chunk မမှန်ပါ"}), 400
    offset = index * rec["chunk_size"]
    if offset + len(data) > rec["size"]:
        return jsonify({"error": "chunk မမှန်ပါ — ဖိုင် အရွယ် "
                                 "ကျော်နေပါတယ်"}), 400
    with _UPLOADS_LOCK:
        if _UPLOADS.get(uid) is not rec:
            return jsonify({"error": "upload မတွေ့ပါ — အစက ပြန် "
                                     "တင်ပါ"}), 400
        with open(rec["path"], "r+b") as out:
            out.seek(offset)
            out.write(data)
        rec["lens"][index] = len(data)
        rec["touched"] = time.time()
        got = sum(rec["lens"].values())
    return jsonify({"ok": True, "received": got})


@app.post("/api/upload/finish")
def upload_finish():
    """Close an upload: the assembly must be exact (every
    chunk present at its exact length, the file's size
    equal to the declaration — a short middle chunk leaves
    a hole the bare size could not catch) and readable by
    the same ffprobe gate the direct path runs. Success
    returns the unguessable {upload_token} the consuming
    posts carry instead of the file part."""
    me = _current_user()
    data = request.get_json(force=True, silent=True) or {}
    uid = (data.get("upload_id") or "").strip()
    with _UPLOADS_LOCK:
        rec = _UPLOADS.get(uid)
    if rec is None:
        return jsonify({"error": "upload မတွေ့ပါ — အစက ပြန် "
                                 "တင်ပါ"}), 400
    if not _upload_owner_ok(rec, me):
        return jsonify({"error": "ဒီ upload က သင့်ဟာ "
                                 "မဟုတ်ပါ"}), 403
    if rec.get("state") == "ready" and rec.get("token"):
        return jsonify({"upload_token": rec["token"]})
    size, cs = rec["size"], rec["chunk_size"]
    n = -(-size // cs)
    lens = rec["lens"]
    complete = len(lens) == n and all(
        lens.get(i) == cs for i in range(n - 1)) \
        and lens.get(n - 1) == size - (n - 1) * cs
    actual = rec["path"].stat().st_size \
        if Path(rec["path"]).exists() else -1
    if not complete or actual != size:
        return jsonify({"error": "ဖိုင် မပြည့်စုံပါ — ပြန် "
                                 "တင်ပါ"}), 400
    from pipeline.common import probe_streams
    try:
        dur = probe_duration(rec["path"])
        streams = probe_streams(rec["path"])
        has_video = any(st.get("codec_type") == "video"
                        for st in streams)
    except Exception:
        dur, has_video = 0, False
    if not dur or dur <= 0 or not has_video:
        return jsonify({"error": "ဖိုင် ကို ဗီဒီယို အဖြစ် ဖတ်လို့ "
                                 "မရပါ — ဗီဒီယို ဖိုင် အမှန် "
                                 "ပြန်တင်ပါ"}), 400
    token = uuid.uuid4().hex
    with _UPLOADS_LOCK:
        rec["token"] = token
        rec["state"] = "ready"
        rec["touched"] = time.time()
        _UPLOAD_TOKENS[token] = rec["id"]
    return jsonify({"upload_token": token})


@app.get("/api/upload/<uid>/status")
def upload_status(uid):
    """Build 243: which chunks of an in-progress upload
    the server already holds, so a client whose page was
    refreshed (or whose tab Android killed mid-upload)
    can resume instead of starting from zero. The answer
    comes straight from the record's per-chunk length
    map — the same source of truth upload_finish verifies
    against. Ownership is judged exactly like chunk and
    finish; asking also counts as activity, so a resume
    in progress is never reaped mid-flight."""
    me = _current_user()
    with _UPLOADS_LOCK:
        rec = _UPLOADS.get(uid)
        if rec is None:
            return jsonify({"error": "upload မတွေ့ပါ — အစက ပြန် "
                                     "တင်ပါ"}), 400
        if not _upload_owner_ok(rec, me):
            return jsonify({"error": "ဒီ upload က သင့်ဟာ "
                                     "မဟုတ်ပါ"}), 403
        rec["touched"] = time.time()
        return jsonify({
            "received": sorted(rec["lens"].keys()),
            "chunk_size": rec["chunk_size"],
            "size": rec["size"],
            "state": rec["state"],
        })


class _TokenUpload:
    """Marker _request_upload returns when the file arrived
    via chunked upload: it carries only the token. Nothing
    reads .filename/.stream before _accept_upload resolves
    it into a _StagedUpload — resolution lives there
    because that is where the caller's account is in hand
    for the ownership check."""

    def __init__(self, token):
        self._upload_token = token
        self.filename = ""


class _StagedUpload:
    """FileStorage-shaped adapter over a finished chunked
    upload, so _accept_upload consumes it exactly like a
    posted file (same stream copy, same probe, same quote
    check). _chunked_record marks it for spending once the
    copy into the job dir has succeeded."""

    def __init__(self, rec):
        self.filename = rec["name"]
        self.stream = open(rec["path"], "rb")
        self._chunked_record = rec


# ---------- web video-file upload (build 215, item 7) ----------
# Both web video flows (the dub wizard and Recap) accept a video
# FILE as the source, not only a link — the Telegram bot has
# accepted files all along; the web was link-only until now. The
# route saves the upload into the job's own directory under a
# FIXED safe name (never the client filename; extension
# whitelist), probes it, and only then registers the job with
# source_path pointing at the saved file, so phase 1 copies it
# and the download stage is skipped entirely (timings.download
# is stamped 0). Admission mirrors a link job exactly: login via
# the normal guard, the quote/balance check happens BEFORE any
# processing (duration from ffprobe on the saved file), and any
# refusal deletes the uploaded file. The 2 GiB ceiling is the
# download chain's own MAX_DOWNLOAD_BYTES. Because the file
# lives in the job dir, the single 24h retention rule (item 3)
# deletes it with everything else.
UPLOAD_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v")


def _form_data():
    """Parse a multipart form into the request-style dict the
    JSON routes eat: fx/subs arrive as JSON strings, the numeric
    knobs as plain strings."""
    data = {}
    for k in request.form:
        v = request.form.get(k)
        if k in ("fx", "subs"):
            try:
                data[k] = json.loads(v)
            except Exception:
                data[k] = {}
        elif k in ("voice_rate", "voice_pitch"):
            try:
                data[k] = int(v)
            except (TypeError, ValueError):
                data[k] = 0
        elif k == "speed_cap":
            try:
                data[k] = float(v)
            except (TypeError, ValueError):
                data[k] = 1.3
        else:
            data[k] = v
    return data


def _request_upload():
    """(data, file_storage|None): multipart requests carry the
    job fields as form fields plus the video in the "file" part;
    anything else keeps the historical JSON body."""
    if request.files:
        f = request.files.get("file")
        if f is not None and (f.filename or "").strip():
            data = _form_data()
            data["upload"] = True
            return data, f
    # Build 242: a chunked upload posts the same form fields
    # with an upload_token instead of the file part. The
    # marker resolves inside _accept_upload (ownership is
    # judged there, with the caller's account in hand).
    if request.form:
        token = (request.form.get("upload_token") or "").strip()
        if token:
            data = _form_data()
            data["upload"] = True
            return data, _TokenUpload(token)
    return (request.get_json(force=True, silent=True) or {}), None


def _accept_upload(job, file_storage, user):
    """Save + validate + quote-check one uploaded video for a
    not-yet-registered job. Returns None on success (the job's
    source_path / upload fields are set) or a polite Burmese
    error string; on ANY refusal the job dir — file included —
    is deleted and nothing is charged or registered."""
    from pipeline.common import probe_streams
    token = getattr(file_storage, "_upload_token", None)
    if token is not None:
        # Build 242: resolve the chunked-upload token into
        # the staged file (ownership + readiness judged
        # here); from this line on the staged file is
        # consumed exactly like a posted one.
        rec = _upload_for_token(token, user)
        if rec is None:
            return ("တင်ထားတဲ့ ဖိုင် မတွေ့တော့ပါ — ဖိုင် "
                    "ပြန် တင်ပါ")
        file_storage = _StagedUpload(rec)
    name = (file_storage.filename or "").strip()
    ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in UPLOAD_EXTS:
        return ("ဒီ ဖိုင် အမျိုးအစား မရပါ — mp4, mkv, mov, avi, "
                "webm ဖိုင် ပဲ တင်လို့ရပါတယ်")
    workdir = JOBS_DIR / job["id"]
    workdir.mkdir(parents=True, exist_ok=True)
    dest = workdir / ("upload" + ext)
    cap = dl_mod.MAX_DOWNLOAD_BYTES
    total = 0
    too_big = False
    try:
        with open(dest, "wb") as out:
            stream = file_storage.stream
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > cap:
                    too_big = True
                    break
                out.write(chunk)
    except Exception:
        shutil.rmtree(workdir, ignore_errors=True)
        return "ဖိုင် တင်တာ မအောင်မြင်ပါ — ပြန်စမ်းပါ"
    if too_big:
        shutil.rmtree(workdir, ignore_errors=True)
        return ("ဖိုင် အရွယ် ကြီးလွန်းပါတယ် — အများဆုံး 2 GB "
                "ပဲ တင်လို့ရပါတယ်")
    try:
        dur = probe_duration(dest)
        streams = probe_streams(dest)
        has_video = any(st.get("codec_type") == "video"
                        for st in streams)
    except Exception:
        dur, has_video = 0, False
    if not dur or dur <= 0 or not has_video:
        shutil.rmtree(workdir, ignore_errors=True)
        return ("ဖိုင် ကို ဗီဒီယို အဖြစ် ဖတ်လို့ မရပါ — "
                "ဗီဒီယို ဖိုင် အမှန် ပြန်တင်ပါ")
    job["source_path"] = str(dest)
    job["upload"] = True
    job["upload_name"] = name[:120]
    # Balance pre-check BEFORE processing, exactly like the
    # preview-reuse pre-check for link jobs (audit B5): the
    # duration is known from the probe, so refuse up front
    # instead of burning server work the quote would reject.
    if ACCOUNTS_ENFORCED and user and user["role"] != "admin":
        mins = max(1, math.ceil(dur / 60.0))
        need = _credits_for_minutes(job, mins)
        if (user["credits"] or 0) < need:
            shutil.rmtree(workdir, ignore_errors=True)
            return (f"credit မလောက်ပါ — ဒီ video အတွက် {need} "
                    f"credits လိုပါတယ်, လက်ကျန် {user['credits']} "
                    "credits ပဲ ရှိပါတယ်")
    rec = getattr(file_storage, "_chunked_record", None)
    if rec is not None:
        # Build 242: the job now owns its copy in the job
        # dir — spend the token and drop the staged
        # original, so a token can never create two jobs.
        try:
            file_storage.stream.close()
        except Exception:
            pass
        _spend_upload(rec)
    return None


def _new_job(data, user=None):
    """Build a job record from request-style data, or (None, error).
    Shared by the web form and the Telegram bot, so a job is a job
    however it arrives. The caller registers it in JOBS and decides
    when its worker starts.

    `user` is the caller's account (web session user / the bot
    chat's gate subject) — build 206 judges an ElevenLabs premium
    pick against the access gate with it. user=None means the
    caller has no gate subject (accounts not enforced, trusted
    internal call): the pre-206 behaviour, no gate applied."""
    url = (data.get("url") or "").strip()
    mode = data.get("mode") or "both"
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
    # Voice style (build 200): the job carries a STYLE key
    # (voice_style, or the voice field holding a key) resolved
    # through the one shared table in pipeline.tts — six styles:
    # the two my-MM voices × မူလ/±8Hz pitch variants. A chosen
    # style's preset pitch/rate are authoritative (both surfaces
    # send the tuning knobs as 0). A legacy plain voice id still
    # works: it means that voice's မူလ variant and the explicit
    # tuning knobs above keep applying, exactly as before. An
    # unknown value fails safe to the product default style
    # (Thiha မူလ). No voice at all keeps the historical default
    # (tts.DEFAULT_VOICE, မူလ variant, knobs applying).
    style_in = data.get("voice_style") or data.get("voice")
    el_entry = None
    gm_entry = None
    if style_in and elevenlabs_tts.is_el_voice(str(style_in).strip()):
        # Build 205: an ElevenLabs premium pick ("el:<voice_id>").
        # It must still resolve for this caller — a removed
        # voice refuses the job HERE with a clear message instead
        # of failing mid-render, and it NEVER falls back to an
        # edge-tts style: the customer picked (and pays a premium
        # for) THIS voice. Build 212: resolution goes through
        # _resolve_el_entry — registry voices keep the build-206
        # gate; the caller's OWN clone resolves on ownership.
        el_entry, el_blocked = _resolve_el_entry(
            user, elevenlabs_tts.voice_id_of(str(style_in).strip()))
        if el_entry is None:
            return None, ("ရွေးထားတဲ့ ElevenLabs အသံ မရှိတော့ပါ — "
                          "အသံ ပြန်ရွေးပေးပါ")
        if el_blocked:
            # Build 206: THE enforcement — hiding the picker
            # group is not enough; a premium pick from an
            # account outside the gate (test mode: not an
            # admin, not a listed test username) refuses the
            # job here, on both surfaces, before any quote.
            return None, ("ဒီ Premium အသံ က အခု စမ်းသပ်ဆဲ "
                          "ဖြစ်လို့ သတ်မှတ်ထားတဲ့ account တွေ ပဲ "
                          "သုံးလို့ရပါသေးတယ် — မကြာခင် အားလုံး "
                          "အတွက် ဖွင့်ပေးပါမယ်")
        voice = "el:" + el_entry["id"]
        voice_style = voice
        voice_rate, voice_pitch = 0, 0
    elif style_in and gemini_tts.is_gm_voice(str(style_in).strip()):
        # Build 229: a Gemini pick ("gm:<Name>"). It must
        # resolve against the 30-voice table — an unknown
        # name refuses the job HERE, and it NEVER falls back
        # to an edge-tts style: the customer picked THIS
        # voice. No access gate (a standard-rate group).
        gm_entry = gemini_tts.find_voice(
            gemini_tts.voice_name_of(str(style_in).strip()))
        if gm_entry is None:
            return None, ("ရွေးထားတဲ့ Gemini အသံ မရှိတော့ပါ — "
                          "အသံ ပြန်ရွေးပေးပါ")
        voice = "gm:" + gm_entry["name"]
        voice_style = voice
        voice_rate, voice_pitch = 0, 0
    elif style_in and str(style_in).strip() in tts.STYLE_BY_KEY:
        style = tts.resolve_style(style_in)
        voice = style["voice"]
        voice_style = style["key"]
        voice_rate, voice_pitch = style["rate"], style["pitch"]
    elif style_in:
        style = tts.resolve_style(style_in)
        voice = style["voice"]
        voice_style = style["key"]
    else:
        voice = tts.DEFAULT_VOICE
        voice_style = tts.resolve_style(voice)["key"]
    # Dub speed cap: how far the retime step may speed speech up to
    # fit its window before it must freeze the frame instead
    # (owner's control, 1.0 = never speed up, up to 4.0).
    try:
        speed_cap = max(1.0, min(4.0, float(data.get("speed_cap") or 1.3)))
    except (TypeError, ValueError):
        speed_cap = 1.3
    # Download quality (build 167): "hd" = original/best rendition
    # (the default — the owner's standing wish), "720"/"480" cap the
    # source height. Anything else falls back to "hd".
    quality = data.get("quality") if data.get("quality") in \
        ("hd", "720", "480") else "hd"
    karaoke = _karaoke_from_data(data)
    if font not in FONT_LABELS:
        return None, "font မမှန်ပါ"
    if data.get("upload"):
        # Build 215 (item 7): upload-sourced — there is no link
        # to validate; the route validates the FILE itself
        # (extension, size, ffprobe, balance) in _accept_upload.
        url = ""
    elif not url.startswith(("http://", "https://")):
        return None, "လင့်ခ် မမှန်ပါ"
    elif not _public_url_ok(url):
        return None, "ဒီ လင့်ခ် ကို သုံးလို့ မရပါ"
    if mode not in ("subs", "dub", "both"):
        return None, "mode မမှန်ပါ"
    # Build 209: at most MAX_BLUR_BOXES blur regions per job —
    # refused here, before the job (and any charge) exists, in
    # the same style as the validations above.
    _boxes_in = (data.get("fx") or {}).get("blur_boxes")
    if isinstance(_boxes_in, list) \
            and len(_boxes_in) > fx_mod.MAX_BLUR_BOXES:
        return None, ("ဝါးမည့် အကွက် အများဆုံး ၅ ခု ပဲ ရပါတယ်"
                      " — အကွက် လျှော့ပြီး ပြန်စမ်းပါ")
    job = {
        "id": uuid.uuid4().hex[:12], "url": url, "mode": mode,
        "voice": voice, "voice_style": voice_style,
        "font": font, "subs": subs, "karaoke": karaoke,
        "voice_rate": voice_rate, "voice_pitch": voice_pitch,
        "speed_cap": speed_cap, "quality": quality,
        "fx": _build_fx(data), "source_path": None,
        "status": "queued", "current": None, "error": None,
        "created_at": time.time(), "updated_at": time.time(),
        "finished_at": None,
        "steps": [{"key": k, "label": STEP_LABELS[k], "state": "pending"}
                  for k in run_job.steps_for_mode(mode)],
    }
    if data.get("upload"):
        job["upload"] = True
    if el_entry is not None:
        # Build 205: record the premium engine + the display name
        # on the job itself (the segments record the same facts
        # per segment at synthesis time).
        job["tts_engine"] = "elevenlabs"
        job["voice_name"] = el_entry["name"]
    if gm_entry is not None:
        # Build 229: record the Gemini engine + display name
        # on the job (the segments record the same facts at
        # synthesis time).
        job["tts_engine"] = "gemini"
        job["voice_name"] = gm_entry["name"]
    return job, None


@app.post("/api/jobs")
def create_job():
    # Build 215 (item 7): a multipart body means the source is an
    # uploaded file; JSON keeps the historical link behaviour.
    data, upload_file = _request_upload()
    # Build 206: the session user is resolved BEFORE _new_job so
    # the ElevenLabs access gate can judge a premium pick (the
    # setup/daily gates below reuse the same `me`).
    me = _current_user()
    job, err = _new_job(data, user=me)
    if err:
        return jsonify({"error": err}), 400
    if me:
        # Build 187: one-time setup gate — refused before the job
        # exists (and before has_job is marked), like the daily
        # limit below.
        setup_msg = _setup_gate_message(me)
        if setup_msg:
            return jsonify({"error": setup_msg}), 400
        job["user_id"] = me["id"]
        accounts.mark_has_job(me["id"])
        # Daily limit (build 180): refused HERE, before the job —
        # and therefore before any charge — even exists.
        limit_msg = _daily_limit_message(me)
        if limit_msg:
            return jsonify({"error": limit_msg}), 400
    preview_id = data.get("preview_id")
    if preview_id:
        # A preview may be reused ONLY for the very link it was taken
        # from (its id is a hash of that link). A stale id left over
        # from a previous video must never substitute the old video
        # for the new link's job. Build 203 (audit V08): the id may
        # be the URL+quality keyed pid for THIS job's quality, or
        # the legacy URL-only pid — a legacy entry still has to
        # pass the _src_valid + _preview_quality gate below.
        expected_pid, _ = _preview_dir(job["url"], job["quality"])
        legacy_pid, _ = _preview_dir(job["url"])
        if str(preview_id) not in (expected_pid, legacy_pid):
            preview_id = None
    if preview_id:
        cand = PREVIEWS_DIR / str(preview_id) / "source.mp4"
        # Build 167: reuse the cached preview only when it was
        # fetched at the quality this job picked — a 720p preview
        # must not silently cap an "hd" job (the blur complaint).
        # Build 183: also require the file to be a COMPLETE,
        # probeable video (_src_valid, not mere existence) — a
        # truncated cache file must fall back to a fresh download
        # in phase 1, never poison the job.
        if _src_valid(cand) \
                and _preview_quality(cand.parent) == job["quality"]:
            job["source_path"] = str(cand)
    if ACCOUNTS_ENFORCED and me and me["role"] != "admin" \
            and job.get("source_path"):
        # Web pre-check (audit B5): when the preview already knows
        # the duration, refuse up front like the bot does instead of
        # burning minutes of server work before the post-phase-1
        # quote check fails the job anyway.
        try:
            _mins = max(1, math.ceil(
                probe_duration(job["source_path"]) / 60.0))
        except Exception:
            _mins = None
        _need = _mins * _credits_per_min_for(job) if _mins else None
        if _need and (me["credits"] or 0) < _need:
            return jsonify({
                "error": f"credit မလောက်ပါ — ဒီ video အတွက် {_need} "
                         f"credits လိုပါတယ်, လက်ကျန် {me['credits']} "
                         "credits ပဲ ရှိပါတယ်"}), 400
    if upload_file is not None:
        # Save + probe + balance-check the file BEFORE the job is
        # registered or queued; a refusal deletes the file and no
        # job ever exists (charge-on-success is untouched).
        uerr = _accept_upload(job, upload_file, me)
        if uerr:
            return jsonify({"error": uerr}), 400
    JOBS[job["id"]] = job
    _save(job)
    # Build 180: queue for a worker slot instead of starting inline —
    # two jobs run at once, the rest wait their turn (never refused).
    _enqueue_job(job)
    return jsonify(job)


def _job_view(job):
    view = {k: job.get(k) for k in
            ("id", "url", "mode", "status", "current", "error", "tg",
             "created_at", "updated_at", "finished_at", "cleanup_at",
             # Build 210: studio (Text-to-Voice) jobs share this
             # listing — the UI labels them from these fields.
             "type", "voice_label", "duration", "story_topic",
             "title_tags")}
    # Build 213: per-stage timings ride the view (owner/admin
    # only ever see them — _owns gates every caller).
    view["timings"] = _timings_of(job)
    view["upload"] = bool(job.get("upload"))  # build 215
    if job.get("status") == "queued":
        # 1-based place among waiting jobs, so the UI/bot can SAY
        # "you are in the queue" instead of looking stuck (build 180).
        waiting = [qid for qid, _kind in _JOB_QUEUE]
        if job["id"] in waiting:
            view["queue_pos"] = waiting.index(job["id"]) + 1
    return view


def _all_jobs():
    """Every job this process knows, newest first.

    Build 204 (audit V11): the in-memory JOBS index is
    authoritative for reads. The pre-204 version globbed and
    parsed EVERY job.json on disk on every call — list views,
    the latest-job poll, the daily-count admission check, the
    done-cap enforcement — and the "read" mutated: it healed
    disk-only jobs, re-armed their cleanup timers and deleted
    expired ones inline. That recovery work now lives in
    _scan_disk_jobs(), which runs once at startup and again from
    the daily backup loop; this function is a pure memory read
    (owner filtering, ordering and queue_pos are computed by the
    callers/_job_view exactly as before)."""
    res = list(JOBS.values())
    res.sort(key=lambda j: j.get("created_at") or 0, reverse=True)
    return res


def _scan_disk_jobs():
    """Recovery pass over jobs/ — the disk half of the pre-204
    _all_jobs, run at startup and from the periodic backup loop
    instead of on every list request.

    A job that exists only on disk cannot be running (the process
    that ran it is gone), so a disk-only 'active' status is healed
    to error rather than lying to the UI — and blocking the gate —
    forever. Finished disk-only jobs are loaded and whatever is
    left of their retention window is re-armed (cleanup timers
    are in-memory, so a restart would otherwise strand them);
    one whose window already passed is deleted at once. Jobs
    already in memory are this process's own and are skipped —
    in a single-process app, disk-only jobs can only appear
    across a restart."""
    for p in sorted(JOBS_DIR.glob("*/job.json")):
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        jid = j.get("id")
        if not jid or jid in JOBS or _tombstoned(jid):
            continue
        if j.get("status") in ("queued", "running"):
            j["status"] = "error"
            j["error"] = "server ပြန်စ၍ ရပ်သွား"
            j["finished_at"] = j.get("finished_at") or time.time()
            JOBS[jid] = j
            _save(j)
            _schedule_cleanup(j)
            if j.get("tg_chat_id"):
                # A queued bot job died with the restart — the
                # customer must be TOLD, not left waiting forever.
                _tg_error_async(j)
        elif j.get("status") in ("done", "error", "cancelled"):
            JOBS[jid] = j
            if jid not in CLEANUP_TIMERS:
                fin = j.get("finished_at") or j.get("created_at") or time.time()
                remain = _job_retention(j) - (time.time() - float(fin))
                if remain <= 0:
                    _cleanup_job(jid)
                    continue
                t = threading.Timer(remain, _cleanup_job, args=(jid,))
                t.daemon = True
                t.start()
                CLEANUP_TIMERS[jid] = t
                j["cleanup_at"] = time.time() + remain
        else:
            # waiting_review / waiting_script / anything else:
            # parked, not finished — load as-is, no timer (the
            # pre-204 read did the same). Build 215: cancelled
            # moved into the finished branch above.
            JOBS[jid] = j


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
    if job.get("status") == "queued":
        waiting = [q for q, _kind in _JOB_QUEUE]
        if jid in waiting:
            job = dict(job, queue_pos=waiting.index(jid) + 1)
    return jsonify(job)


def _load_job(jid):
    job = JOBS.get(jid)
    if not job:
        if _tombstoned(jid):
            # Build 204: deleted jobs stay deleted — never reload
            # a directory cleanup already removed (or is removing).
            return None
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
    if not job or not _owns(_current_user(), job):
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
    if not job or not _owns(_current_user(), job):
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
    # Build 180: the re-render queues for a worker slot like any
    # other job (it used to jump the queue with its own thread).
    job["status"] = "queued"
    _save(job)
    _enqueue_job(job, kind="rerun")
    return jsonify({"ok": True, "edited_count": changed})


def _job_output_path(jid):
    """Which file a download/stream serves (build 179): ?v=small
    serves the compact copy when the job has one; anything else —
    including no parameter, which is what older clients and the
    Telegram 'full quality on the web' pointer rely on — serves
    the full master. Build 198: the copy is LAZY — a ?v=small
    request for a job that has none yet generates it on the spot
    (seconds; _small_on_demand never raises), and only a failed
    or pointless generation falls back to the full master, so
    the main download always delivers a video."""
    jdir = JOBS_DIR / jid
    # Build 210: a Text-to-Voice studio job's output is audio —
    # output.mp3 with no output.mp4 beside it. Serve it under the
    # same download/stream routes (and the same ownership check).
    mp3 = jdir / "output.mp3"
    if mp3.exists() and not (jdir / "output.mp4").exists():
        return mp3, f"myanmar-{jid}.mp3"
    if request.args.get("v") == "small":
        small = jdir / "output_small.mp4"
        if not small.exists():
            _small_on_demand(jid)
        if small.exists():
            return small, f"myanmar-{jid}-small.mp4"
    return jdir / "output.mp4", f"myanmar-{jid}.mp4"


@app.get("/api/jobs/<jid>/download")
def download_job(jid):
    job = _load_job(jid)
    if not job or not _owns(_current_user(), job):
        return jsonify({"error": "မပြီးသေးပါ"}), 404
    out, name = _job_output_path(jid)
    if not out.exists():
        return jsonify({"error": "မပြီးသေးပါ"}), 404
    # conditional=True (build 159): Range requests get 206 partial
    # content — downloads can resume instead of restarting, and
    # download managers / the phone browser can pull in parallel.
    return send_file(out, as_attachment=True,
                     download_name=name,
                     conditional=True)


@app.get("/api/jobs/<jid>/stream")
def stream_job(jid):
    """Inline playback for the result preview (the download route's
    attachment header prevents some phone browsers from playing it).
    Defaults to the full master; ?v=small previews the compact copy."""
    job = _load_job(jid)
    if not job or not _owns(_current_user(), job):
        return jsonify({"error": "မပြီးသေးပါ"}), 404
    out, _name = _job_output_path(jid)
    if not out.exists():
        return jsonify({"error": "မပြီးသေးပါ"}), 404
    return send_file(out, as_attachment=False, conditional=True)


def _stop_and_cleanup(job):
    """Build 204 (audit V09) — the active half of a cancel, run on
    its own thread so the DELETE request returns at once:
    terminate the job's registered process groups (SIGTERM, grace,
    SIGKILL — see pipeline.common.terminate_job_processes), wait
    — bounded — for the worker thread to actually exit, and only
    then remove the files. If the worker somehow never exits (a
    long in-process step with no subprocess to kill), the files
    are LEFT for delete_job's 300 s failsafe rather than deleted
    under a live writer; the tombstone in _cleanup_job plus the
    cancel guards in _worker/_finish keep the state stable
    either way."""
    jid = job["id"]
    try:
        terminate_job_processes(jid, grace=5.0)
    except Exception:
        pass
    wt = _WORKER_THREADS.get(jid)
    if wt is not None and wt is not threading.current_thread():
        wt.join(timeout=30)
        # A step that spawned its process just as the first sweep
        # passed gets the same treatment before files are touched.
        try:
            terminate_job_processes(jid, grace=2.0)
        except Exception:
            pass
        wt.join(timeout=10)
        if wt.is_alive():
            return
    _cleanup_job(jid)


@app.delete("/api/jobs/<jid>")
def delete_job(jid):
    """Delete a job and all its files right now (the user asked for no
    leftovers; otherwise the 24-hour auto-cleanup does it)."""
    job = _load_job(jid)
    if not job or not _owns(_current_user(), job):
        return jsonify({"error": "job မတွေ့ပါ"}), 404
    if job["status"] in ("queued", "running"):
        # Stop it instead of refusing. Build 204 (audit V09): the
        # cancel no longer waits for the next step boundary — the
        # job's running processes are terminated immediately and
        # the files are removed once the worker has exited. The
        # 300 s timer remains as a failsafe if a step never
        # returns at all.
        job["cancel_requested"] = True
        _save(job)
        threading.Thread(target=_stop_and_cleanup, args=(job,),
                         daemon=True).start()
        t = threading.Timer(300, _cleanup_job, args=(jid,))
        t.daemon = True
        t.start()
        return jsonify({"ok": True, "cancelling": True})
    old_timer = CLEANUP_TIMERS.pop(jid, None)
    if old_timer:
        old_timer.cancel()
    _cleanup_job(jid)
    return jsonify({"ok": True})


# ---------- Text-to-Voice studio (build 210) ----------
# Burma-Voice-style studio: the customer types (or SRT-imports)
# Burmese text, picks any voice from the same library the dub
# wizard uses, and gets one mp3. Synthesis reuses the dub
# engines verbatim — the text is chunked into a segments_my.json
# in the job folder and pipeline.tts / pipeline.elevenlabs_tts
# synthesize it exactly like a dub (same contracts, same loud
# failure doctrine: an ElevenLabs failure is NEVER silently
# re-voiced with edge-tts). The record is a job (type "tts"),
# so the single 24h retention rule (_schedule_cleanup), the
# jobs listing and the download/stream routes serve it like
# any other job. Charge-on-success only, mirroring
# _charge_for_job: the quoted estimate is checked up front
# (admins exempt), the actual charge is ceil(actual output
# minutes) x the voice's per-minute rate, taken only once the
# mp3 exists; a failure charges zero.

TTS_TEXT_CAP = 5000          # characters, per studio request
TTS_CHARS_PER_MIN = 700      # estimate basis (matches the UI)
TTS_CHUNK_CHARS = 450        # per-engine-call chunk ceiling
TTS_SPEEDS = (0.8, 1.0, 1.2, 1.5, 2.0, 3.0, 4.0)


def _atempo_chain(speed):
    """ffmpeg atempo filter chain for a playback speed. A single
    atempo instance only accepts 0.5..2.0, so speeds above 2.0
    chain instances (build 215: studio speeds now reach 4.0x —
    4.0 -> "atempo=2.0,atempo=2.0", 3.0 -> "atempo=2.0,atempo=1.5").
    Speeds at or below 2.0 keep the historical single filter."""
    parts = []
    s = float(speed)
    while s > 2.0:
        parts.append(2.0)
        s /= 2.0
    parts.append(round(s, 4))
    return ",".join(f"atempo={p:g}" for p in parts)


def _tts_chunks(text, limit=TTS_CHUNK_CHARS):
    """Split studio text into engine-sized chunks: sentence
    boundaries first (Burmese ။, !, ?, …, and '.'+space), then
    greedy packing up to `limit` chars; a single over-long
    sentence is hard-split at a space when one is near."""
    parts = [p.strip() for p in
             re.split(r"(?<=[။!?…])\s*|\n+|(?<=\.)\s+", text)]
    parts = [p for p in parts if p]
    units = []
    for p in parts:
        while len(p) > limit:
            cut = p.rfind(" ", 0, limit)
            if cut < limit // 2:
                cut = limit
            units.append(p[:cut].strip())
            p = p[cut:].strip()
        if p:
            units.append(p)
    chunks, cur = [], ""
    for u in units:
        if cur and len(cur) + 1 + len(u) > limit:
            chunks.append(cur)
            cur = u
        else:
            cur = f"{cur} {u}".strip() if cur else u
    if cur:
        chunks.append(cur)
    return chunks


def _tts_estimate_credits(text, rate):
    """Up-front estimate, the same math the studio UI shows:
    minutes = chars / 700, billed per STARTED minute."""
    return max(1, math.ceil(len(text) / TTS_CHARS_PER_MIN)) * rate


def _tts_charge(job, due):
    """Success-only studio charge — _charge_for_job's exact
    semantics for a job whose quote is computed at finish time
    (from the real output duration): take the full amount, or
    what is there plus a recorded shortfall; admins and
    anonymous jobs are never charged."""
    uid = job.get("user_id")
    if not uid or job.get("charged_minutes"):
        return
    user = accounts.get_user(uid)
    if not user or user["role"] == "admin":
        return
    paid = due
    if not accounts.charge(uid, due, job["id"]):
        paid = max(0, min(user["credits"] or 0, due))
        if paid:
            accounts.charge(uid, paid, job["id"])
        if paid < due:
            job["charge_shortfall"] = due - paid
    job["charged_minutes"] = paid
    _maybe_referral_reward(job)  # build 221


def _tts_view(job):
    return {"id": job["id"], "type": "tts",
            "status": job.get("status"), "error": job.get("error"),
            "voice": job.get("voice"),
            "voice_label": job.get("voice_label"),
            "chars": job.get("chars"), "speed": job.get("speed"),
            "duration": job.get("duration"),
            "charged": job.get("charged_minutes"),
            "created_at": job.get("created_at"),
            "finished_at": job.get("finished_at"),
            "timings": _timings_of(job),  # build 213
            "download": f"/api/jobs/{job['id']}/download",
            "stream": f"/api/jobs/{job['id']}/stream"}


def _run_tts_job(job, text, speed):
    """Synthesize + assemble + charge one studio job. Raises on
    any failure (the caller records the error; nothing has been
    charged at that point)."""
    from pipeline.common import load_segments, save_segments
    _timing_start(job)  # build 213: the studio run is one span
    workdir = JOBS_DIR / job["id"]
    workdir.mkdir(parents=True, exist_ok=True)
    segs, t = [], 0.0
    for chunk in _tts_chunks(text):
        # Nominal window per chunk (only used by the free
        # engine's FAILED-silent last resort to keep a timeline;
        # the real durations are measured per file afterwards).
        dur = max(1.0, len(chunk) * 60.0 / TTS_CHARS_PER_MIN)
        segs.append({"start": round(t, 3),
                     "end": round(t + dur, 3), "text_my": chunk})
        t += dur
    save_segments(segs, workdir / "segments_my.json")
    _tts_t0 = time.monotonic()  # build 213: synthesis stage
    if job.get("tts_engine") == "elevenlabs":
        elevenlabs_tts.synthesize(
            workdir, elevenlabs_tts.voice_id_of(job["voice"]),
            voice_name=job.get("voice_name"))
    elif job.get("tts_engine") == "gemini":
        # Build 229: a Gemini pick synthesizes through the
        # key pool at natural speed — the studio speed is
        # applied ONCE on the assembled audio below, for
        # every engine (the build-215 rule).
        gemini_tts.synthesize(
            workdir, gemini_tts.voice_name_of(job["voice"]))
    else:
        style = tts.resolve_style(job["voice_style"])
        # Build 215: the studio speed is applied ONCE, on the
        # assembled audio below (chained atempo), for BOTH
        # engines — the pre-215 standard path baked it into the
        # edge-tts rate percent (+300% at 4.0x, which edge-tts
        # cannot honour reliably), and the ElevenLabs path used
        # a single atempo that tops out at 2.0. Synthesis now
        # always runs at the style's own preset rate/pitch.
        tts.synthesize(workdir, voice=style["voice"],
                       rate=style["rate"], pitch=style["pitch"])
    job.setdefault("timings", {})["tts"] = round(
        time.monotonic() - _tts_t0, 1)
    segs = load_segments(workdir / "segments_my.json")
    files = [s["tts_file"] for s in segs if s.get("tts_file")]
    if not files:
        raise RuntimeError(
            "အသံ ထုတ်တာ မရပါ — TTS service တွေ ခဏ အလုပ်မလုပ်ပါ။ "
            "ခဏနေမှ ပြန်စမ်းပါ (credit လုံးဝ မဖြတ်ပါ)")
    listfile = workdir / "concat.txt"
    listfile.write_text("".join(f"file '{p}'\n" for p in files),
                        encoding="utf-8")
    out = workdir / "output.mp3"
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "concat",
           "-safe", "0", "-i", str(listfile)]
    if speed != 1.0:
        # Build 215: one speed application for both engines, on
        # the assembled audio — chained atempo, so 3.0x and 4.0x
        # are real (a single atempo caps at 2.0). The measured
        # duration below (and therefore the charge) follows the
        # sped-up file automatically.
        cmd += ["-filter:a", _atempo_chain(speed)]
    cmd += ["-c:a", "libmp3lame", "-q:a", "3", str(out)]
    sh(cmd)
    dur = probe_duration(out)
    job["duration"] = round(dur, 3)
    job["output_size"] = out.stat().st_size
    minutes = max(1, math.ceil(dur / 60.0))
    if job.get("tts_engine") == "elevenlabs":
        rate = elevenlabs_tts.credits_per_min()
    elif job.get("tts_engine") == "gemini":
        # Build 229: the Gemini rate, read live at charge
        # time (GEMINI_CREDITS_PER_MIN, default 1).
        rate = gemini_tts.credits_per_min()
    else:
        rate = 1
    job["quote_minutes"] = minutes
    job["quote_credits"] = minutes * rate
    _tts_charge(job, minutes * rate)
    _timing_close(job)  # build 213
    job["status"] = "done"
    job["finished_at"] = time.time()
    _save(job)
    _schedule_cleanup(job)


@app.post("/api/tts")
def create_tts():
    user = _current_user()
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "စာသား အရင် ထည့်ပါ"}), 400
    if len(text) > TTS_TEXT_CAP:
        return jsonify({"error":
                        f"စာသား အရှည်ဆုံး {TTS_TEXT_CAP} လုံး ပဲ "
                        "ရပါတယ် — လျှော့ပြီး ပြန်စမ်းပါ"}), 400
    try:
        speed = float(data.get("speed") or 1.0)
    except (TypeError, ValueError):
        speed = 1.0
    if speed not in TTS_SPEEDS:
        speed = 1.0
    # Voice resolution mirrors _new_job exactly (builds 200 /
    # 205 / 206 / 212): an ElevenLabs pick must resolve for the
    # caller through _resolve_el_entry — registry voices gated,
    # the caller's own clone on ownership; a style key resolves
    # through the shared table; anything else fails safe to the
    # product default style.
    voice_in = data.get("voice")
    el_entry = None
    gm_entry = None
    if voice_in and elevenlabs_tts.is_el_voice(str(voice_in).strip()):
        el_entry, el_blocked = _resolve_el_entry(
            user, elevenlabs_tts.voice_id_of(str(voice_in).strip()))
        if el_entry is None:
            return jsonify({"error":
                            "ရွေးထားတဲ့ ElevenLabs အသံ မရှိတော့ပါ — "
                            "အသံ ပြန်ရွေးပေးပါ"}), 400
        if el_blocked:
            return jsonify({"error":
                            "ဒီ Premium အသံ က အခု စမ်းသပ်ဆဲ "
                            "ဖြစ်လို့ သတ်မှတ်ထားတဲ့ account တွေ ပဲ "
                            "သုံးလို့ရပါသေးတယ် — မကြာခင် အားလုံး "
                            "အတွက် ဖွင့်ပေးပါမယ်"}), 403
        voice = "el:" + el_entry["id"]
        voice_style = voice
        voice_label = el_entry["name"]
        rate = elevenlabs_tts.credits_per_min()
    elif voice_in and gemini_tts.is_gm_voice(str(voice_in).strip()):
        # Build 229: a Gemini pick — the GEMINI_CREDITS_PER_MIN
        # rate (default 1), read live like the premium rate.
        gm_entry = gemini_tts.find_voice(
            gemini_tts.voice_name_of(str(voice_in).strip()))
        if gm_entry is None:
            return jsonify({"error":
                            "ရွေးထားတဲ့ Gemini အသံ မရှိတော့ပါ — "
                            "အသံ ပြန်ရွေးပေးပါ"}), 400
        voice = "gm:" + gm_entry["name"]
        voice_style = voice
        voice_label = gm_entry["name"]
        rate = gemini_tts.credits_per_min()
    else:
        style = tts.resolve_style(voice_in)
        voice = style["voice"]
        voice_style = style["key"]
        voice_label = style["label"]
        rate = 1
    # Admission: a non-admin account must cover the ESTIMATE
    # (started minutes x rate) before any synthesis runs —
    # nothing is deducted here; the real charge lands on
    # success, from the measured duration.
    uid = user["id"] if user else None
    if uid and user["role"] != "admin":
        acct = accounts.get_user(uid)
        est = _tts_estimate_credits(text, rate)
        if acct and (acct["credits"] or 0) < est:
            return jsonify({"error":
                            f"credit မလောက်ပါ — ဒီ အသံ အတွက် "
                            f"ခန့်မှန်း {est} credits လိုပါတယ်, "
                            f"လက်ကျန် {acct['credits']} credits ပဲ "
                            "ရှိပါတယ်"}), 400
    now = time.time()
    job = {"id": uuid.uuid4().hex[:12], "type": "tts",
           "url": None, "mode": "tts",
           "voice": voice, "voice_style": voice_style,
           "voice_label": voice_label,
           "text": text, "chars": len(text), "speed": speed,
           "user_id": uid, "status": "running", "current": "tts",
           "error": None, "created_at": now, "updated_at": now,
           "finished_at": None, "steps": [],
           # Build 213: studio jobs never queue — the wait is 0.
           "timings": {"queue_s": 0.0}}
    if el_entry is not None:
        job["tts_engine"] = "elevenlabs"
        job["voice_name"] = el_entry["name"]
    if gm_entry is not None:
        job["tts_engine"] = "gemini"
        job["voice_name"] = gm_entry["name"]
    JOBS[job["id"]] = job
    _save(job)
    try:
        _run_tts_job(job, text, speed)
    except Exception as e:
        _timing_close(job)  # build 213: keep the partial span
        job["status"] = "error"
        job["error"] = str(e)[-1500:]
        job["finished_at"] = time.time()
        _save(job)
        _schedule_cleanup(job)
        return jsonify({"error": job["error"]}), 500
    return jsonify(_tts_view(job))


@app.get("/api/tts/history")
def tts_history():
    """The caller's studio outputs, newest first — the studio's
    🕘 list. Ownership is the same _owns() rule as every other
    job surface."""
    user = _current_user()
    mine = [j for j in _all_jobs()
            if j.get("type") == "tts" and _owns(user, j)]
    return jsonify({"items": [_tts_view(j) for j in mine[:20]]})


# ---------- Recap product (build 211) ----------
# Burma-Voice-style Recap: paste a link -> a ready Burmese recap
# video of the WHOLE video (အစအဆုံး), narrated in a chosen
# voice. Two phases, mirroring the dub pipeline's split:
# phase 1 (worker kind "recap1") downloads + transcribes
# through the same guarded chain as a dub job, admission-quotes
# the job (started SOURCE minutes x the voice's per-minute
# rate, _quote_for_job), then pipeline.recap writes the
# narration script from the transcript through the translator's
# own LLM gateway chain. The job parks at status
# "waiting_script" — the translator's review philosophy: the
# script is the one thing worth reading before credits move.
# POST /api/recap/<id>/render stores the final (possibly
# edited) text and queues phase 2 (kind "recap2"): the script
# is chunked into proportional timed segments and rendered by
# the UNCHANGED dub phase 2 in mode "both" — TTS narration,
# retime fits it to the video (same speed caps as dub), recap
# lines burned as cinematic subtitles, original audio off.
# The record is a normal job (type "recap"), so retention, the
# jobs listing and the download/stream routes serve it.
# Charge-on-success only: _finish is the single charge point;
# a phase-1 failure, a script failure, an insufficient balance
# at quote time or a render failure charges exactly zero.

RECAP_STEPS = ["download", "transcribe", "script",
               "tts", "retime", "finalize"]
RECAP_TEXT_CAP = 20000       # chars, for the render-time edit


def _recap_view(job):
    view = {k: job.get(k) for k in
            ("id", "type", "url", "status", "current", "error",
             "voice", "voice_label", "quote_minutes",
             "quote_credits", "charged_minutes", "recap_edited",
             "title_tags",
             "created_at", "finished_at")}
    view["timings"] = _timings_of(job)  # build 213
    view["upload"] = bool(job.get("upload"))  # build 215
    view["steps"] = job.get("steps") or []
    if job.get("recap_text") is not None:
        view["recap_text"] = job["recap_text"]
    if job.get("status") == "queued":
        waiting = [q for q, _k in _JOB_QUEUE]
        if job["id"] in waiting:
            view["queue_pos"] = waiting.index(job["id"]) + 1
    return view


def _recap_worker(job):
    """Phase 1: download -> transcribe -> quote -> script.
    Parks the job at waiting_script; nothing is charged."""
    workdir = JOBS_DIR / job["id"]
    threading.Thread(target=_heartbeat, args=(job,),
                     daemon=True).start()
    with job_context(job["id"]):
        try:
            job["status"] = "running"
            _timing_start(job)  # build 213
            _save(job)
            run_job.run_recap_phase1(
                workdir, job["url"], progress=_progress_fn(job),
                quality=job.get("quality") or "hd",
                source_path=job.get("source_path"))
            if job.get("upload"):
                job.setdefault("timings", {})["download"] = 0
            _quote_for_job(job)
            progress = _progress_fn(job)
            progress("script", "running")
            target = recap_mod.target_chars_for(workdir)
            script, provider = recap_mod.generate_script(
                workdir, target)
            progress("script", "done")
            job["recap_text"] = script
            job["recap_provider"] = provider
            job["recap_edited"] = False
            job["status"] = "waiting_script"
            job["current"] = None
            # Build 213: phase 1's machine time ends at the
            # park — the human review wait that follows is not
            # processing time (see _timing_close).
            _timing_close(job)
            _save(job)
            # Admin usage board: count this video under the
            # gateway that wrote its script (best-effort, like
            # the translation count in run_phase1).
            try:
                from pipeline import api_stats
                api_stats.record(
                    [{"text": "recap", "provider": provider}])
            except Exception:
                pass
        except JobCancelled:
            _cancelled(job)
        except Exception as e:
            if job.get("cancel_requested"):
                _cancelled(job)
            else:
                _fail(job, e)


def _recap_worker2(job):
    """Phase 2: the final script -> proportional timed segments
    -> the dub phase 2 in mode "both" -> _finish (the only
    place a recap job is ever charged)."""
    workdir = JOBS_DIR / job["id"]
    threading.Thread(target=_heartbeat, args=(job,),
                     daemon=True).start()
    with job_context(job["id"]):
        try:
            job["status"] = "running"
            _timing_start(job)  # build 213
            _save(job)
            from pipeline.common import save_segments
            total = probe_duration(workdir / "source.mp4")
            segs = recap_mod.script_segments(
                job.get("recap_text") or "", total)
            if not segs:
                raise RuntimeError(
                    "Recap စာသား မရှိပါ — စာသား ပြန်ထည့်ပြီး "
                    "ပြန်စမ်းပါ (credit လုံးဝ မဖြတ်ပါ)")
            save_segments(segs, workdir / "segments_my.json")
            run_job.run_phase2(
                workdir, mode="both", voice=job["voice"],
                progress=_progress_fn(job), fx=job.get("fx"),
                font=job.get("font") or "noto",
                voice_rate=job.get("voice_rate", 0),
                voice_pitch=job.get("voice_pitch", 0),
                subs=job.get("subs"),
                max_speed=job.get("speed_cap") or 1.3,
                quality=job.get("quality") or "hd",
                voice_name=job.get("voice_name"),
                karaoke=bool(job.get("karaoke")))
            _finish(job)
        except JobCancelled:
            _cancelled(job)
        except Exception as e:
            if job.get("cancel_requested"):
                _cancelled(job)
            else:
                _fail(job, e)


@app.post("/api/recap")
def create_recap():
    user = _current_user()
    # Build 215 (item 7): multipart = uploaded file source, the
    # same _accept_upload gate as the dub wizard.
    data, upload_file = _request_upload()
    url = (data.get("url") or "").strip()
    if upload_file is not None:
        url = ""
    elif not url.startswith(("http://", "https://")):
        return jsonify({"error": "လင့်ခ် မမှန်ပါ"}), 400
    elif not _public_url_ok(url):
        return jsonify({"error": "ဒီ လင့်ခ် ကို သုံးလို့ မရပါ"}), 400
    # Voice resolution mirrors the studio / _new_job exactly
    # (builds 200 / 205 / 206 / 212): an ElevenLabs pick must
    # resolve for the caller through _resolve_el_entry —
    # registry voices gated, the caller's own clone on
    # ownership; a style key resolves through the shared
    # table; anything else fails safe to the product default
    # style.
    voice_in = data.get("voice")
    el_entry = None
    gm_entry = None
    if voice_in and elevenlabs_tts.is_el_voice(str(voice_in).strip()):
        el_entry, el_blocked = _resolve_el_entry(
            user, elevenlabs_tts.voice_id_of(str(voice_in).strip()))
        if el_entry is None:
            return jsonify({"error":
                            "ရွေးထားတဲ့ ElevenLabs အသံ မရှိတော့ပါ — "
                            "အသံ ပြန်ရွေးပေးပါ"}), 400
        if el_blocked:
            return jsonify({"error":
                            "ဒီ Premium အသံ က အခု စမ်းသပ်ဆဲ "
                            "ဖြစ်လို့ သတ်မှတ်ထားတဲ့ account တွေ ပဲ "
                            "သုံးလို့ရပါသေးတယ် — မကြာခင် အားလုံး "
                            "အတွက် ဖွင့်ပေးပါမယ်"}), 403
        voice = "el:" + el_entry["id"]
        voice_style = voice
        voice_label = el_entry["name"]
        voice_rate, voice_pitch = 0, 0
    elif voice_in and gemini_tts.is_gm_voice(str(voice_in).strip()):
        # Build 229: a Gemini pick ("gm:<Name>") — resolves
        # against the 30-voice table; no gate (a standard-rate
        # group, open once a key is configured).
        gm_entry = gemini_tts.find_voice(
            gemini_tts.voice_name_of(str(voice_in).strip()))
        if gm_entry is None:
            return jsonify({"error":
                            "ရွေးထားတဲ့ Gemini အသံ မရှိတော့ပါ — "
                            "အသံ ပြန်ရွေးပေးပါ"}), 400
        voice = "gm:" + gm_entry["name"]
        voice_style = voice
        voice_label = gm_entry["name"]
        voice_rate, voice_pitch = 0, 0
    else:
        style = tts.resolve_style(voice_in)
        voice = style["voice"]
        voice_style = style["key"]
        voice_label = style["label"]
        voice_rate, voice_pitch = style["rate"], style["pitch"]
    quality = data.get("quality") if data.get("quality") in \
        ("hd", "720", "480") else "hd"
    karaoke = _karaoke_from_data(data)
    try:
        speed_cap = max(1.0, min(4.0,
                                 float(data.get("speed_cap") or 1.3)))
    except (TypeError, ValueError):
        speed_cap = 1.3
    # Build 226 (audit F-01): the Recap form's blur editor —
    # the boxes ride in as data["fx"] and normalize through
    # the same _build_fx path as the dub wizard. Before, fx
    # was hardcoded None here and in _recap_worker2, so the
    # original burned-in captions always survived.
    fx, fx_err = _fx_from_data(data)
    if fx_err:
        return jsonify({"error": fx_err}), 400
    now = time.time()
    job = {"id": uuid.uuid4().hex[:12], "type": "recap",
           "url": url, "mode": "recap",
           "voice": voice, "voice_style": voice_style,
           "voice_label": voice_label,
           "voice_rate": voice_rate, "voice_pitch": voice_pitch,
           "speed_cap": speed_cap, "quality": quality,
           "karaoke": karaoke,
           "font": "noto", "subs": _build_subs(data), "fx": fx,
           "source_path": None,
           "status": "queued", "current": None, "error": None,
           "created_at": now, "updated_at": now,
           "finished_at": None,
           "steps": [{"key": k, "label": STEP_LABELS[k],
                      "state": "pending"} for k in RECAP_STEPS]}
    if el_entry is not None:
        job["tts_engine"] = "elevenlabs"
        job["voice_name"] = el_entry["name"]
    if gm_entry is not None:
        job["tts_engine"] = "gemini"
        job["voice_name"] = gm_entry["name"]
    if user:
        # Same admission gates as a dub job, in the same order:
        # one-time setup, then the daily limit — both refuse
        # before the job exists (and before any charge).
        setup_msg = _setup_gate_message(user)
        if setup_msg:
            return jsonify({"error": setup_msg}), 400
        job["user_id"] = user["id"]
        accounts.mark_has_job(user["id"])
        limit_msg = _daily_limit_message(user)
        if limit_msg:
            return jsonify({"error": limit_msg}), 400
    if upload_file is not None:
        uerr = _accept_upload(job, upload_file, user)
        if uerr:
            return jsonify({"error": uerr}), 400
    JOBS[job["id"]] = job
    _save(job)
    _enqueue_job(job, kind="recap1")
    return jsonify(_recap_view(job))


@app.get("/api/recap/<jid>")
def get_recap(jid):
    job = _load_job(jid)
    if not job or job.get("type") != "recap" \
            or not _owns(_current_user(), job):
        return jsonify({"error": "job မတွေ့ပါ"}), 404
    return jsonify(_recap_view(job))


@app.post("/api/recap/<jid>/render")
def render_recap(jid):
    """Submit the final recap text (edited or untouched) and
    queue the render. Only a job parked at waiting_script can
    be rendered — the quote from phase 1 stands, and the charge
    still lands only on a finished video."""
    job = _load_job(jid)
    if not job or job.get("type") != "recap" \
            or not _owns(_current_user(), job):
        return jsonify({"error": "job မတွေ့ပါ"}), 404
    if job["status"] != "waiting_script":
        return jsonify({"error":
                        "ဒီ job က စာသား အတည်ပြုရန် အဆင့် "
                        "မဟုတ်ပါ"}), 409
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Recap စာသား အရင် ထည့်ပါ"}), 400
    if len(text) > RECAP_TEXT_CAP:
        return jsonify({"error":
                        f"စာသား အရှည်ဆုံး {RECAP_TEXT_CAP} လုံး "
                        "ပဲ ရပါတယ် — လျှော့ပြီး ပြန်စမ်းပါ"}), 400
    if text != (job.get("recap_text") or ""):
        job["recap_edited"] = True
    job["recap_text"] = text
    # A render supersedes any pending auto-cleanup (a parked
    # job has none today; the mirror of job_review stays exact).
    old_timer = CLEANUP_TIMERS.pop(jid, None)
    if old_timer:
        old_timer.cancel()
    job.pop("cleanup_at", None)
    job["status"] = "queued"
    _save(job)
    _enqueue_job(job, kind="recap2")
    return jsonify({"ok": True})



# ---------- Story Video product (build 216) ----------
# Topic (or a pasted story) -> an AI Burmese narration script
# + a shot plan over the renderer's FIXED cartoon asset
# vocabulary -> the script parks at "waiting_script" for the
# customer's review/edit (zero charged) -> render: the script
# is narrated by the chosen voice and pipeline.story_render
# draws the cartoon (code-drawn flat 2D cast over painted
# sets, slow zoompan, cinematic subtitles) into a vertical
# MP4. Two phases mirroring Recap exactly (worker kinds
# "story1" / "story2"); the record is a normal job (type
# "story"), so the jobs listing, download/stream routes and
# the single 24h retention rule serve it unchanged.
# Charge-on-success only, like every product: the quote is
# the TARGET length's started minutes x the voice's rate
# (checked at phase 1 and again before the render), and
# _finish charges the FINISHED video's started minutes x
# the rate — _charge_for_job is the single charge point.

STORY_STEPS = ["script", "tts", "render"]
STORY_TEXT_CAP = 20000       # chars, pasted story / final edit
STORY_TOPIC_CAP = 300        # chars, for the topic line
STORY_MINUTES = (1, 2, 3)    # offered target lengths
_STORY_RENDER_SEM = threading.Semaphore(1)


def _story_steps(mode="topic"):
    # Build 228 (story-text audit F1): in own-text mode the
    # customer's words are used verbatim — the first step
    # plans the cartoon scenes, it does not "write text",
    # and the label must not claim it does.
    first = "Scene ခွဲရန်" if mode == "text" \
        else "Story စာသား ရေးရန်"
    return [{"key": "script", "label": first,
             "state": "pending"},
            {"key": "tts", "label": STEP_LABELS["tts"],
             "state": "pending"},
            {"key": "render", "label": STEP_LABELS["render"],
             "state": "pending"}]


def _story_view(job):
    view = {k: job.get(k) for k in
            ("id", "type", "status", "current", "error",
             "voice", "voice_label", "quote_minutes",
             "quote_credits", "charged_minutes", "story_edited",
             "story_topic", "story_minutes", "story_mode",
             "story_music",
             "story_karaoke", "story_style", "title_tags",
             "created_at", "finished_at")}
    view["timings"] = _timings_of(job)  # build 213
    view["steps"] = job.get("steps") or []
    if job.get("story_text") is not None:
        view["story_text"] = job["story_text"]
    if job.get("status") == "queued":
        waiting = [q for q, _k in _JOB_QUEUE]
        if job["id"] in waiting:
            view["queue_pos"] = waiting.index(job["id"]) + 1
    return view


def _quote_for_story(job):
    """Admission quote for a story job — _quote_for_job's
    contract with the TARGET length standing in for the
    source duration (a story job has no source video): a
    non-admin account must cover the target's started
    minutes x the voice's per-minute rate (1 standard; the
    premium rate for ElevenLabs voices). Nothing is
    deducted here — credits move only in _finish."""
    uid = job.get("user_id")
    if not uid:
        return
    user = accounts.get_user(uid)
    if not user or user["role"] == "admin":
        return
    minutes = int(job.get("story_minutes") or 1)
    job["quote_minutes"] = minutes
    needed = minutes * _credits_per_min_for(job)
    job["quote_credits"] = needed
    _save(job)
    if (user["credits"] or 0) < needed:
        raise RuntimeError(
            f"credit မလောက်ပါ — ဒီ video အတွက် {needed} credits "
            f"လိုပါတယ်, လက်ကျန် {user['credits']} credits ပဲ "
            "ရှိပါတယ်")


def _story_worker(job):
    """Phase 1: quote -> script + shot plan -> park at
    waiting_script; nothing is charged. A pasted story skips
    script generation (the words are the customer's own) —
    only its shot plan is AI work, with a built-in fallback
    plan when every gateway is down."""
    threading.Thread(target=_heartbeat, args=(job,),
                     daemon=True).start()
    with job_context(job["id"]):
        try:
            job["status"] = "running"
            _timing_start(job)  # build 213
            _save(job)
            _quote_for_story(job)
            progress = _progress_fn(job)
            progress("script", "running")
            if job.get("story_text"):
                shots, provider = story_mod.plan_for_text(
                    job["story_text"],
                    minutes=int(job.get("story_minutes") or 1))
            else:
                target = int(job.get("story_minutes") or 1) \
                    * story_mod.CHARS_PER_MIN
                script, shots, provider = \
                    story_mod.generate_story(
                        job.get("story_topic") or "", target)
                job["story_text"] = script
            job["story_shots"] = shots
            job["story_provider"] = provider
            progress("script", "done")
            job["story_edited"] = False
            job["status"] = "waiting_script"
            job["current"] = None
            # The human review park is not machine time
            # (build 213, same as recap).
            _timing_close(job)
            _save(job)
            try:
                from pipeline import api_stats
                api_stats.record(
                    [{"text": "story", "provider": provider}])
            except Exception:
                pass
        except JobCancelled:
            _cancelled(job)
        except Exception as e:
            if job.get("cancel_requested"):
                _cancelled(job)
            else:
                _fail(job, e)


def _story_worker2(job):
    """Phase 2: narrate + render the cartoon, then settle
    the charge on the FINISHED video's started minutes and
    _finish (the only place a story job is ever charged).
    The story-render semaphore (1) keeps a render from
    starving dub/recap jobs of CPU on the shared box."""
    workdir = JOBS_DIR / job["id"]
    threading.Thread(target=_heartbeat, args=(job,),
                     daemon=True).start()
    with job_context(job["id"]):
        try:
            job["status"] = "running"
            _timing_start(job)  # build 213
            _save(job)
            with _STORY_RENDER_SEM:
                story_render.render_story(
                    workdir, job, progress=_progress_fn(job))
            out = workdir / "output.mp4"
            minutes = max(1, math.ceil(
                probe_duration(out) / 60.0))
            job["quote_minutes"] = minutes
            job["quote_credits"] = minutes \
                * _credits_per_min_for(job)
            _finish(job)
        except JobCancelled:
            _cancelled(job)
        except Exception as e:
            if job.get("cancel_requested"):
                _cancelled(job)
            else:
                _fail(job, e)


@app.post("/api/story")
def create_story():
    user = _current_user()
    data = request.get_json(force=True, silent=True) or {}
    topic = (data.get("topic") or "").strip()
    text = (data.get("text") or "").strip()
    # Build 228 (story-text audit F5): the mode is EXPLICIT
    # state sent by the form — never inferred from whichever
    # box happens to be non-empty, so text left in a hidden
    # box can no longer silently override the topic. Only
    # the active mode's input is read; the other is dropped.
    # A caller that sends no mode keeps the pre-228
    # inference, so older clients do not break.
    mode_in = (data.get("mode") or "").strip().lower()
    if mode_in in ("text", "topic"):
        story_mode = mode_in
    else:
        story_mode = "text" if text else "topic"
    if story_mode == "text":
        if not text:
            return jsonify({"error": "စာသား အရင် ထည့်ပါ"}), 400
        if len(text) > STORY_TEXT_CAP:
            return jsonify({"error":
                            f"စာသား အရှည်ဆုံး {STORY_TEXT_CAP} "
                            "လုံး ပဲ ရပါတယ် — လျှော့ပြီး "
                            "ပြန်စမ်းပါ"}), 400
        topic = ""
    else:
        text = ""
        if not topic:
            return jsonify({"error": "ခေါင်းစဉ် အရင် ထည့်ပါ"}), 400
        if len(topic) > STORY_TOPIC_CAP:
            return jsonify({"error":
                            f"ခေါင်းစဉ် အရှည်ဆုံး "
                            f"{STORY_TOPIC_CAP} လုံး ပဲ ရပါတယ်"}), \
                400
    try:
        minutes = int(data.get("minutes") or 1)
    except (TypeError, ValueError):
        minutes = 1
    if minutes not in STORY_MINUTES:
        minutes = 1
    if story_mode == "text":
        # Build 228 (audit F2/F3): a pasted text's length IS
        # its duration — the length chips do not apply (the
        # form hides them in this mode). The estimate, on the
        # app's narration basis (~700 chars per finished
        # minute), replaces the chip figure everywhere: the
        # admission quote, the review-screen quote and the
        # planner's shot density all read story_minutes.
        minutes = max(1, math.ceil(len(text)
                                   / story_mod.CHARS_PER_MIN))
    # Build 226 (audit F-05): the form's speed slider (rate,
    # −50..+100 percent) — read at last. A nonzero value
    # becomes the narration's voice_rate below; 0 / absent
    # keeps the voice style's own preset rate. Before 226 the
    # slider was sent but never read, so dragging it changed
    # nothing.
    try:
        story_rate = int(data.get("rate") or 0)
    except (TypeError, ValueError):
        story_rate = 0
    story_rate = max(-50, min(100, story_rate))
    # Build 227: the Story style pack — the form's style
    # chips send one of the renderer's named grades
    # (normal / whiteboard / newsreel / anime / pixel);
    # absent or unknown keeps the normal look. Stored on
    # the job; the renderer grades the composited scene
    # frames with it and the subtitle burn is untouched.
    story_style = story_render.normalize_style(
        data.get("style"))
    # Voice resolution mirrors create_recap / _new_job exactly
    # (builds 200 / 205 / 206 / 212): an ElevenLabs pick must
    # resolve for the caller through _resolve_el_entry —
    # registry voices gated, the caller's own clone on
    # ownership; a style key resolves through the shared
    # table; anything else fails safe to the product default.
    voice_in = data.get("voice")
    el_entry = None
    gm_entry = None
    if voice_in and elevenlabs_tts.is_el_voice(str(voice_in).strip()):
        el_entry, el_blocked = _resolve_el_entry(
            user, elevenlabs_tts.voice_id_of(str(voice_in).strip()))
        if el_entry is None:
            return jsonify({"error":
                            "ရွေးထားတဲ့ ElevenLabs အသံ မရှိတော့ပါ — "
                            "အသံ ပြန်ရွေးပေးပါ"}), 400
        if el_blocked:
            return jsonify({"error":
                            "ဒီ Premium အသံ က အခု စမ်းသပ်ဆဲ "
                            "ဖြစ်လို့ သတ်မှတ်ထားတဲ့ account တွေ ပဲ "
                            "သုံးလို့ရပါသေးတယ် — မကြာခင် အားလုံး "
                            "အတွက် ဖွင့်ပေးပါမယ်"}), 403
        voice = "el:" + el_entry["id"]
        voice_style = voice
        voice_label = el_entry["name"]
        # Build 228 (audit F4): the speed slider applies to
        # Premium voices too — stored as voice_rate exactly
        # like the built-in branch below, and the renderer
        # applies it to the EL narration (atempo — the same
        # conversion the Studio uses for both engines).
        # Before 228 this branch zeroed it, so the slider
        # was a dead control whenever an EL voice was picked.
        voice_rate, voice_pitch = story_rate, 0
    elif voice_in and gemini_tts.is_gm_voice(str(voice_in).strip()):
        # Build 229: a Gemini pick — and the speed slider
        # applies to it exactly like the EL branch above
        # (build 228): stored as voice_rate, applied by the
        # renderer as an atempo pass.
        gm_entry = gemini_tts.find_voice(
            gemini_tts.voice_name_of(str(voice_in).strip()))
        if gm_entry is None:
            return jsonify({"error":
                            "ရွေးထားတဲ့ Gemini အသံ မရှိတော့ပါ — "
                            "အသံ ပြန်ရွေးပေးပါ"}), 400
        voice = "gm:" + gm_entry["name"]
        voice_style = voice
        voice_label = gm_entry["name"]
        voice_rate, voice_pitch = story_rate, 0
    else:
        style = tts.resolve_style(voice_in)
        voice = style["voice"]
        voice_style = style["key"]
        voice_label = style["label"]
        voice_rate, voice_pitch = style["rate"], style["pitch"]
        if story_rate:
            # Build 226 (F-05): the slider overrides the
            # style's preset rate; 0 kept the preset above.
            voice_rate = story_rate
    now = time.time()
    job = {"id": uuid.uuid4().hex[:12], "type": "story",
           "url": "", "mode": "story",
           "story_topic": topic, "story_minutes": minutes,
           "story_mode": story_mode,
           "story_music": bool(data.get("music")),
           "story_karaoke": bool(data.get("karaoke")),
           "story_style": story_style,
           "story_text": text or None,
           "voice": voice, "voice_style": voice_style,
           "voice_label": voice_label,
           "voice_rate": voice_rate, "voice_pitch": voice_pitch,
           "font": "noto", "subs": _build_subs(data),
           "status": "queued", "current": None, "error": None,
           "created_at": now, "updated_at": now,
           "finished_at": None,
           "steps": _story_steps(story_mode)}
    if el_entry is not None:
        job["tts_engine"] = "elevenlabs"
        job["voice_name"] = el_entry["name"]
    if gm_entry is not None:
        job["tts_engine"] = "gemini"
        job["voice_name"] = gm_entry["name"]
    if user:
        # Same admission gates as a recap job, in the same
        # order — both refuse before the job exists.
        setup_msg = _setup_gate_message(user)
        if setup_msg:
            return jsonify({"error": setup_msg}), 400
        job["user_id"] = user["id"]
        accounts.mark_has_job(user["id"])
        limit_msg = _daily_limit_message(user)
        if limit_msg:
            return jsonify({"error": limit_msg}), 400
    JOBS[job["id"]] = job
    _save(job)
    _enqueue_job(job, kind="story1")
    return jsonify(_story_view(job))


@app.get("/api/story/<jid>")
def get_story(jid):
    job = _load_job(jid)
    if not job or job.get("type") != "story" \
            or not _owns(_current_user(), job):
        return jsonify({"error": "job မတွေ့ပါ"}), 404
    return jsonify(_story_view(job))


@app.post("/api/story/<jid>/render")
def render_story(jid):
    """Submit the final story text (edited or untouched) and
    queue the render. Only a job parked at waiting_script can
    be rendered; the balance is re-checked against the
    target-length estimate here, and the charge still lands
    only on a finished video (at its own started minutes)."""
    job = _load_job(jid)
    if not job or job.get("type") != "story" \
            or not _owns(_current_user(), job):
        return jsonify({"error": "job မတွေ့ပါ"}), 404
    if job["status"] != "waiting_script":
        return jsonify({"error":
                        "ဒီ job က စာသား အတည်ပြုရန် အဆင့် "
                        "မဟုတ်ပါ"}), 409
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Story စာသား အရင် ထည့်ပါ"}), 400
    if len(text) > STORY_TEXT_CAP:
        return jsonify({"error":
                        f"စာသား အရှည်ဆုံး {STORY_TEXT_CAP} လုံး "
                        "ပဲ ရပါတယ် — လျှော့ပြီး ပြန်စမ်းပါ"}), 400
    if job.get("story_mode") == "text":
        # Build 228 (audit F2/F3): refresh the text-length
        # estimate from the FINAL text (the customer may
        # have edited it at the park) so this re-check — like
        # the admission quote — prices the text's own
        # minutes, never a stale chip figure.
        job["story_minutes"] = max(
            1, math.ceil(len(text) / story_mod.CHARS_PER_MIN))
    uid = job.get("user_id")
    if uid:
        acct = accounts.get_user(uid)
        if acct and acct["role"] != "admin":
            needed = int(job.get("story_minutes") or 1) \
                * _credits_per_min_for(job)
            if (acct["credits"] or 0) < needed:
                return jsonify({"error":
                                f"credit မလောက်ပါ — ဒီ video "
                                f"အတွက် {needed} credits လိုပါတယ်, "
                                f"လက်ကျန် {acct['credits']} credits "
                                "ပဲ ရှိပါတယ်"}), 400
    if text != (job.get("story_text") or ""):
        job["story_edited"] = True
    job["story_text"] = text
    old_timer = CLEANUP_TIMERS.pop(jid, None)
    if old_timer:
        old_timer.cancel()
    job.pop("cleanup_at", None)
    job["status"] = "queued"
    _save(job)
    _enqueue_job(job, kind="story2")
    return jsonify({"ok": True})




# ---------- Commentary product (build 217) ----------
# The customer gives a video (link or uploaded file); its
# own speech is transcribed and pipeline.commentary writes
# an energetic Burmese sports/commentary-style script from
# the transcript (play-by-play, present tense) through the
# same free-first LLM chain as Recap/Story. Two phases
# mirroring Recap exactly (worker kinds "commentary1" /
# "commentary2"): phase 1 downloads + transcribes through
# the shared guarded chain, refuses honestly when the
# transcript has fewer than 3 usable segments (v1 invents
# nothing), admission-quotes the job (started SOURCE
# minutes x the commentary rate — COMMENTARY_CREDITS_PER_MIN
# for standard voices, the premium rate + 1 for ElevenLabs
# voices, via _credits_per_min_for), writes the script and
# parks at "waiting_script", zero charged.
# POST /api/commentary/<id>/render stores the final
# (possibly edited) text and queues phase 2: recap's
# proportional segments -> the UNCHANGED dub phase 2 in
# mode "both" (TTS narration, shared retime, cinematic
# subtitle burn of the commentary text) — then, when the
# customer chose the DUCKED original-audio mode (the
# default), pipeline.commentary.mix_bed lays the original
# audio under the narration at ~13% volume, re-timed chunk
# by chunk so it stays in sync with the picture; OFF keeps
# the narration-only track. The record is a normal job
# (type "commentary"), so retention (the single 24h rule),
# the jobs listing and the download/stream routes serve
# it. Charge-on-success only: _finish is the single charge
# point; any failure or cancel charges exactly zero.

COMMENTARY_STEPS = ["download", "transcribe", "script",
                    "tts", "retime", "finalize"]
COMMENTARY_TEXT_CAP = 20000  # chars, for the render-time edit
COMMENTARY_AUDIO_MODES = ("ducked", "off")


def _commentary_steps():
    return [{"key": "download", "label": STEP_LABELS["download"],
             "state": "pending"},
            {"key": "transcribe",
             "label": STEP_LABELS["transcribe"],
             "state": "pending"},
            {"key": "script", "label": "Commentary စာသား ရေးရန်",
             "state": "pending"},
            {"key": "tts", "label": STEP_LABELS["tts"],
             "state": "pending"},
            {"key": "retime", "label": STEP_LABELS["retime"],
             "state": "pending"},
            {"key": "finalize", "label": STEP_LABELS["finalize"],
             "state": "pending"}]


def _commentary_view(job):
    view = {k: job.get(k) for k in
            ("id", "type", "url", "status", "current", "error",
             "voice", "voice_label", "quote_minutes",
             "quote_credits", "charged_minutes",
             "commentary_edited", "commentary_audio", "title_tags",
             "created_at", "finished_at")}
    view["timings"] = _timings_of(job)  # build 213
    view["upload"] = bool(job.get("upload"))  # build 215
    view["steps"] = job.get("steps") or []
    if job.get("commentary_text") is not None:
        view["commentary_text"] = job["commentary_text"]
    if job.get("status") == "queued":
        waiting = [q for q, _k in _JOB_QUEUE]
        if job["id"] in waiting:
            view["queue_pos"] = waiting.index(job["id"]) + 1
    return view


def _commentary_worker(job):
    """Phase 1: download -> transcribe -> segment check ->
    quote -> commentary script. Parks the job at
    waiting_script; nothing is charged."""
    workdir = JOBS_DIR / job["id"]
    threading.Thread(target=_heartbeat, args=(job,),
                     daemon=True).start()
    with job_context(job["id"]):
        try:
            job["status"] = "running"
            _timing_start(job)  # build 213
            _save(job)
            run_job.run_recap_phase1(
                workdir, job["url"], progress=_progress_fn(job),
                quality=job.get("quality") or "hd",
                source_path=job.get("source_path"))
            if job.get("upload"):
                job.setdefault("timings", {})["download"] = 0
            # Edge rule: a video with (almost) no speech has
            # nothing to commentate on — fail loudly here,
            # before any quote or script work, zero charged.
            if commentary_mod.usable_segment_count(workdir) \
                    < commentary_mod.MIN_SEGMENTS:
                raise RuntimeError(commentary_mod.SPARSE_MSG)
            _quote_for_job(job)
            progress = _progress_fn(job)
            progress("script", "running")
            target = recap_mod.target_chars_for(workdir)
            script, provider = commentary_mod.generate_script(
                workdir, target)
            progress("script", "done")
            job["commentary_text"] = script
            job["commentary_provider"] = provider
            job["commentary_edited"] = False
            job["status"] = "waiting_script"
            job["current"] = None
            # The human review park is not machine time
            # (build 213, same as recap).
            _timing_close(job)
            _save(job)
            try:
                from pipeline import api_stats
                api_stats.record(
                    [{"text": "commentary",
                      "provider": provider}])
            except Exception:
                pass
        except JobCancelled:
            _cancelled(job)
        except Exception as e:
            if job.get("cancel_requested"):
                _cancelled(job)
            else:
                _fail(job, e)


def _commentary_worker2(job):
    """Phase 2: the final script -> proportional timed
    segments -> the dub phase 2 in mode "both" -> the
    ducked original-audio bed when chosen -> _finish (the
    only place a commentary job is ever charged)."""
    workdir = JOBS_DIR / job["id"]
    threading.Thread(target=_heartbeat, args=(job,),
                     daemon=True).start()
    with job_context(job["id"]):
        try:
            job["status"] = "running"
            _timing_start(job)  # build 213
            _save(job)
            from pipeline.common import save_segments
            total = probe_duration(workdir / "source.mp4")
            segs = recap_mod.script_segments(
                job.get("commentary_text") or "", total)
            if not segs:
                raise RuntimeError(
                    "Commentary စာသား မရှိပါ — စာသား "
                    "ပြန်ထည့်ပြီး ပြန်စမ်းပါ "
                    "(credit လုံးဝ မဖြတ်ပါ)")
            save_segments(segs, workdir / "segments_my.json")
            run_job.run_phase2(
                workdir, mode="both", voice=job["voice"],
                progress=_progress_fn(job), fx=job.get("fx"),
                font=job.get("font") or "noto",
                voice_rate=job.get("voice_rate", 0),
                voice_pitch=job.get("voice_pitch", 0),
                subs=job.get("subs"),
                max_speed=job.get("speed_cap") or 1.3,
                quality=job.get("quality") or "hd",
                voice_name=job.get("voice_name"),
                karaoke=bool(job.get("karaoke")))
            if job.get("commentary_audio") != "off":
                # Ducked bed (the default): the original
                # audio under the narration at ~13%. The bed
                # is garnish — a mix failure leaves the
                # narration-only output standing and the job
                # still succeeds (Story's music-bed rule).
                progress = _progress_fn(job)
                progress("finalize", "running")
                try:
                    job["commentary_bed"] = bool(
                        commentary_mod.mix_bed(workdir))
                except Exception as bed_err:
                    print(f"[job {job['id']}] commentary bed "
                          f"skipped: {bed_err}", flush=True)
                    job["commentary_bed"] = False
                progress("finalize", "done")
            _finish(job)
        except JobCancelled:
            _cancelled(job)
        except Exception as e:
            if job.get("cancel_requested"):
                _cancelled(job)
            else:
                _fail(job, e)


@app.post("/api/commentary")
def create_commentary():
    user = _current_user()
    # Multipart = uploaded file source, the same
    # _accept_upload gate as the dub wizard / Recap
    # (build 215).
    data, upload_file = _request_upload()
    url = (data.get("url") or "").strip()
    if upload_file is not None:
        url = ""
    elif not url.startswith(("http://", "https://")):
        return jsonify({"error": "လင့်ခ် မမှန်ပါ"}), 400
    elif not _public_url_ok(url):
        return jsonify({"error": "ဒီ လင့်ခ် ကို သုံးလို့ မရပါ"}), 400
    # Voice resolution mirrors create_recap exactly (builds
    # 200 / 205 / 206 / 212): an ElevenLabs pick must resolve
    # for the caller through _resolve_el_entry — registry
    # voices gated, the caller's own clone on ownership; a
    # style key resolves through the shared table; anything
    # else fails safe to the product default style.
    voice_in = data.get("voice")
    el_entry = None
    gm_entry = None
    if voice_in and elevenlabs_tts.is_el_voice(str(voice_in).strip()):
        el_entry, el_blocked = _resolve_el_entry(
            user, elevenlabs_tts.voice_id_of(str(voice_in).strip()))
        if el_entry is None:
            return jsonify({"error":
                            "ရွေးထားတဲ့ ElevenLabs အသံ မရှိတော့ပါ — "
                            "အသံ ပြန်ရွေးပေးပါ"}), 400
        if el_blocked:
            return jsonify({"error":
                            "ဒီ Premium အသံ က အခု စမ်းသပ်ဆဲ "
                            "ဖြစ်လို့ သတ်မှတ်ထားတဲ့ account တွေ ပဲ "
                            "သုံးလို့ရပါသေးတယ် — မကြာခင် အားလုံး "
                            "အတွက် ဖွင့်ပေးပါမယ်"}), 403
        voice = "el:" + el_entry["id"]
        voice_style = voice
        voice_label = el_entry["name"]
        voice_rate, voice_pitch = 0, 0
    elif voice_in and gemini_tts.is_gm_voice(str(voice_in).strip()):
        # Build 229: a Gemini pick ("gm:<Name>") — resolves
        # against the 30-voice table; no gate (a standard-rate
        # group, open once a key is configured).
        gm_entry = gemini_tts.find_voice(
            gemini_tts.voice_name_of(str(voice_in).strip()))
        if gm_entry is None:
            return jsonify({"error":
                            "ရွေးထားတဲ့ Gemini အသံ မရှိတော့ပါ — "
                            "အသံ ပြန်ရွေးပေးပါ"}), 400
        voice = "gm:" + gm_entry["name"]
        voice_style = voice
        voice_label = gm_entry["name"]
        voice_rate, voice_pitch = 0, 0
    else:
        style = tts.resolve_style(voice_in)
        voice = style["voice"]
        voice_style = style["key"]
        voice_label = style["label"]
        voice_rate, voice_pitch = style["rate"], style["pitch"]
    quality = data.get("quality") if data.get("quality") in \
        ("hd", "720", "480") else "hd"
    karaoke = _karaoke_from_data(data)
    try:
        speed_cap = max(1.0, min(4.0,
                                 float(data.get("speed_cap") or 1.3)))
    except (TypeError, ValueError):
        speed_cap = 1.3
    audio_mode = data.get("audio_mode") \
        if data.get("audio_mode") in COMMENTARY_AUDIO_MODES \
        else "ducked"
    # Build 226 (audit F-02): the Commentary form's blur
    # editor — same _build_fx normalization as the dub wizard
    # (fx was hardcoded None here and in _commentary_worker2,
    # so original captions always clashed, unblurred).
    fx, fx_err = _fx_from_data(data)
    if fx_err:
        return jsonify({"error": fx_err}), 400
    now = time.time()
    job = {"id": uuid.uuid4().hex[:12], "type": "commentary",
           "url": url, "mode": "commentary",
           "commentary_audio": audio_mode,
           "voice": voice, "voice_style": voice_style,
           "voice_label": voice_label,
           "voice_rate": voice_rate, "voice_pitch": voice_pitch,
           "speed_cap": speed_cap, "quality": quality,
           "karaoke": karaoke,
           "font": "noto", "subs": _build_subs(data), "fx": fx,
           "source_path": None,
           "status": "queued", "current": None, "error": None,
           "created_at": now, "updated_at": now,
           "finished_at": None,
           "steps": _commentary_steps()}
    if el_entry is not None:
        job["tts_engine"] = "elevenlabs"
        job["voice_name"] = el_entry["name"]
    if gm_entry is not None:
        job["tts_engine"] = "gemini"
        job["voice_name"] = gm_entry["name"]
    if user:
        # Same admission gates as a recap job, in the same
        # order — both refuse before the job exists.
        setup_msg = _setup_gate_message(user)
        if setup_msg:
            return jsonify({"error": setup_msg}), 400
        job["user_id"] = user["id"]
        accounts.mark_has_job(user["id"])
        limit_msg = _daily_limit_message(user)
        if limit_msg:
            return jsonify({"error": limit_msg}), 400
    if upload_file is not None:
        uerr = _accept_upload(job, upload_file, user)
        if uerr:
            return jsonify({"error": uerr}), 400
    JOBS[job["id"]] = job
    _save(job)
    _enqueue_job(job, kind="commentary1")
    return jsonify(_commentary_view(job))


@app.get("/api/commentary/<jid>")
def get_commentary(jid):
    job = _load_job(jid)
    if not job or job.get("type") != "commentary" \
            or not _owns(_current_user(), job):
        return jsonify({"error": "job မတွေ့ပါ"}), 404
    return jsonify(_commentary_view(job))


@app.post("/api/commentary/<jid>/render")
def render_commentary(jid):
    """Submit the final commentary text (edited or untouched)
    and queue the render. Only a job parked at waiting_script
    can be rendered — the quote from phase 1 stands, and the
    charge still lands only on a finished video."""
    job = _load_job(jid)
    if not job or job.get("type") != "commentary" \
            or not _owns(_current_user(), job):
        return jsonify({"error": "job မတွေ့ပါ"}), 404
    if job["status"] != "waiting_script":
        return jsonify({"error":
                        "ဒီ job က စာသား အတည်ပြုရန် အဆင့် "
                        "မဟုတ်ပါ"}), 409
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "Commentary စာသား အရင် ထည့်ပါ"}), 400
    if len(text) > COMMENTARY_TEXT_CAP:
        return jsonify({"error":
                        f"စာသား အရှည်ဆုံး {COMMENTARY_TEXT_CAP} လုံး "
                        "ပဲ ရပါတယ် — လျှော့ပြီး ပြန်စမ်းပါ"}), 400
    if text != (job.get("commentary_text") or ""):
        job["commentary_edited"] = True
    job["commentary_text"] = text
    old_timer = CLEANUP_TIMERS.pop(jid, None)
    if old_timer:
        old_timer.cancel()
    job.pop("cleanup_at", None)
    job["status"] = "queued"
    _save(job)
    _enqueue_job(job, kind="commentary2")
    return jsonify({"ok": True})


# ---------- Captions-only product (build 219) ----------
# The cheap tier (roadmap item 3): video in -> download ->
# transcribe -> translate to Burmese through the SAME chain
# as the dub flow (run_phase1 style "subs": same prompts,
# same majority-untranslated guard — a guard failure is the
# loud Burmese error, zero charged) -> the job parks at
# "waiting_review" with the Burmese segments editable, zero
# charged -> confirm (POST /api/captions/<id>/render applies
# the edits exactly like the main flow's job_review) ->
# phase 2 burns the final segments into the ORIGINAL video
# through the shared cinematic burn (run_phase2 mode "subs":
# original audio untouched, NO TTS; build 226 added the
# review-stage blur boxes, drawn on the real frame and sent
# with the render request) -> the
# done screen offers BOTH downloads: the subtitled MP4 (the
# normal job route) and the Burmese .srt (subs_my.srt,
# regenerated from the final edited segments at render time
# so the file and the burn are the same text). The record is
# a normal job (type "captions"), so retention (the single
# 24h rule), the jobs listing and the download/stream routes
# serve it. NO voice picker, NO ElevenLabs anywhere.
# Pricing: half the dub rate — CAPTIONS_CREDITS_PER_MIN
# (default 0.5) through _credits_for_minutes, so the quote,
# the upload pre-check and the charge agree exactly:
# ceil(started SOURCE minutes x rate), minimum 1 credit.
# Charge-on-success only: _finish is the single charge
# point; any failure or cancel charges exactly zero.
# Build 220: the view's Karaoke tick (job["karaoke"]) makes
# phase 2 burn with the karaoke subtitle style (per-word
# highlight) — a free style option, same price either way.

CAPTIONS_STEPS = ["download", "transcribe", "translate",
                  "finalize"]


def _captions_view(job):
    view = {k: job.get(k) for k in
            ("id", "type", "url", "status", "current", "error",
             "quote_minutes", "quote_credits", "charged_minutes",
             "edited_count", "karaoke", "title_tags",
             "created_at", "finished_at")}
    view["timings"] = _timings_of(job)  # build 213
    view["upload"] = bool(job.get("upload"))  # build 215
    view["steps"] = job.get("steps") or []
    if job.get("status") == "queued":
        waiting = [q for q, _k in _JOB_QUEUE]
        if job["id"] in waiting:
            view["queue_pos"] = waiting.index(job["id"]) + 1
    return view


def _captions_worker(job):
    """Phase 1: download -> transcribe -> translate (the
    shared guarded chain) -> quote -> park at
    waiting_review; nothing is charged."""
    workdir = JOBS_DIR / job["id"]
    threading.Thread(target=_heartbeat, args=(job,),
                     daemon=True).start()
    with job_context(job["id"]):
        try:
            job["status"] = "running"
            _timing_start(job)  # build 213
            _save(job)
            run_job.run_phase1(
                workdir, job["url"],
                source_path=job.get("source_path"),
                progress=_progress_fn(job), style="subs",
                quality=job.get("quality") or "hd",
                force_source=bool(job.get("upload")))
            if job.get("upload"):
                # An uploaded source has no download stage
                # (build 215, same as the dub worker).
                job.setdefault("timings", {})["download"] = 0
            _quote_for_job(job)
            job["status"] = "waiting_review"
            job["current"] = None
            # The human review park is not machine time
            # (build 213, same as recap).
            _timing_close(job)
            _save(job)
        except JobCancelled:
            _cancelled(job)
        except Exception as e:
            if job.get("cancel_requested"):
                _cancelled(job)
            else:
                _fail(job, e)


def _captions_worker2(job):
    """Phase 2: regenerate subs_my.srt from the FINAL edited
    segments (so the .srt download and the burn carry the
    same text), burn them into the original video with its
    audio untouched, then _finish (the only place a captions
    job is ever charged)."""
    workdir = JOBS_DIR / job["id"]
    threading.Thread(target=_heartbeat, args=(job,),
                     daemon=True).start()
    with job_context(job["id"]):
        try:
            job["status"] = "running"
            _timing_start(job)  # build 213
            _save(job)
            from pipeline.common import load_segments, write_srt
            segs = load_segments(workdir / "segments_my.json")
            if not segs:
                raise RuntimeError(
                    "စာတန်း မရှိပါ — ပြန်စမ်းပါ "
                    "(credit လုံးဝ မဖြတ်ပါ)")
            write_srt(segs, workdir / "subs_my.srt")
            run_job.run_phase2(
                workdir, mode="subs", progress=_progress_fn(job),
                font=job.get("font") or "noto", subs=None,
                fx=job.get("fx"),
                quality=job.get("quality") or "hd",
                karaoke=bool(job.get("karaoke")))
            _finish(job)
        except JobCancelled:
            _cancelled(job)
        except Exception as e:
            if job.get("cancel_requested"):
                _cancelled(job)
            else:
                _fail(job, e)


@app.post("/api/captions")
def create_captions():
    user = _current_user()
    # Multipart = uploaded file source, the same
    # _accept_upload gate as the dub wizard / Recap /
    # Commentary (build 215).
    data, upload_file = _request_upload()
    url = (data.get("url") or "").strip()
    if upload_file is not None:
        url = ""
    elif not url.startswith(("http://", "https://")):
        return jsonify({"error": "လင့်ခ် မမှန်ပါ"}), 400
    elif not _public_url_ok(url):
        return jsonify({"error": "ဒီ လင့်ခ် ကို သုံးလို့ မရပါ"}), 400
    quality = data.get("quality") if data.get("quality") in \
        ("hd", "720", "480") else "hd"
    karaoke = _karaoke_from_data(data)
    # Karaoke subtitle style (build 220): a free style option,
    # ticked in the Captions view; JSON carries a bool, the
    # multipart form a string.
    _kar = data.get("karaoke")
    karaoke = _kar is True or (isinstance(_kar, str)
                               and _kar.strip().lower()
                               in ("1", "true", "on", "yes"))
    # Build 226 (audit F-03): creation-time blur boxes are
    # accepted too, normalized through the same _build_fx
    # path — but the primary path is the review stage, where
    # the boxes are drawn on the real downloaded frame and
    # arrive with the render request (see render_captions).
    # Before 226 fx was hardcoded None and phase 2 never
    # received any, so original captions always showed
    # through the product sold to cover them.
    fx, fx_err = _fx_from_data(data)
    if fx_err:
        return jsonify({"error": fx_err}), 400
    now = time.time()
    job = {"id": uuid.uuid4().hex[:12], "type": "captions",
           "url": url, "mode": "captions",
           "quality": quality, "font": "noto", "fx": fx,
           "karaoke": karaoke,
           "source_path": None,
           "status": "queued", "current": None, "error": None,
           "created_at": now, "updated_at": now,
           "finished_at": None,
           "steps": [{"key": k, "label": STEP_LABELS[k],
                      "state": "pending"} for k in CAPTIONS_STEPS]}
    if user:
        # Same admission gates as a recap/commentary job, in
        # the same order — both refuse before the job exists.
        setup_msg = _setup_gate_message(user)
        if setup_msg:
            return jsonify({"error": setup_msg}), 400
        job["user_id"] = user["id"]
        accounts.mark_has_job(user["id"])
        limit_msg = _daily_limit_message(user)
        if limit_msg:
            return jsonify({"error": limit_msg}), 400
    if upload_file is not None:
        uerr = _accept_upload(job, upload_file, user)
        if uerr:
            return jsonify({"error": uerr}), 400
    JOBS[job["id"]] = job
    _save(job)
    _enqueue_job(job, kind="captions1")
    return jsonify(_captions_view(job))


@app.get("/api/captions/<jid>")
def get_captions(jid):
    job = _load_job(jid)
    if not job or job.get("type") != "captions" \
            or not _owns(_current_user(), job):
        return jsonify({"error": "job မတွေ့ပါ"}), 404
    return jsonify(_captions_view(job))


@app.post("/api/captions/<jid>/render")
def render_captions(jid):
    """Confirm the reviewed subtitles and queue the burn.

    Body: {"edits": [{"idx": 0, "text_my": "..."}, ...]} —
    applied to segments_my.json exactly like the main flow's
    job_review (unlisted segments keep their translated
    text; an edit that empties a line is ignored). Only a
    job parked at waiting_review can be confirmed — the
    quote from phase 1 stands, and the charge still lands
    only on a finished video."""
    job = _load_job(jid)
    if not job or job.get("type") != "captions" \
            or not _owns(_current_user(), job):
        return jsonify({"error": "job မတွေ့ပါ"}), 404
    if job["status"] != "waiting_review":
        return jsonify({"error":
                        "ဒီ job က စာတန်း အတည်ပြုရန် အဆင့် "
                        "မဟုတ်ပါ"}), 409
    data = request.get_json(force=True, silent=True) or {}
    # Build 226 (audit F-03): blur boxes drawn on the real
    # frame during the review park arrive WITH this request —
    # normalized through the same _build_fx path and stored
    # on the job, so _captions_worker2 burns them in. A
    # request without fx keeps whatever creation stored
    # (usually none). A bad fx refuses BEFORE any edit is
    # applied, so the park survives for a corrected retry.
    if isinstance(data.get("fx"), dict):
        fx, fx_err = _fx_from_data(data)
        if fx_err:
            return jsonify({"error": fx_err}), 400
        job["fx"] = fx
    edits = {}
    for e in data.get("edits") or []:
        if not isinstance(e, dict):
            continue
        try:
            if "idx" in e and "text_my" in e:
                edits[int(e["idx"])] = str(e["text_my"])
        except (TypeError, ValueError):
            continue
    from pipeline.common import load_segments, save_segments
    from pipeline.common import write_srt
    seg_path = JOBS_DIR / jid / "segments_my.json"
    segs = load_segments(seg_path)
    if not segs:
        return jsonify({"error": "စာတန်း မရှိသေးပါ"}), 409
    changed = 0
    for idx, new_text in edits.items():
        if 0 <= idx < len(segs) and new_text.strip() \
                and new_text.strip() != \
                (segs[idx].get("text_my") or "").strip():
            segs[idx]["text_my"] = new_text.strip()
            segs[idx]["edited"] = True
            changed += 1
    save_segments(segs, seg_path)
    write_srt(segs, JOBS_DIR / jid / "subs_my.srt")
    job["edited_count"] = changed
    # A render supersedes any pending auto-cleanup (a parked
    # job has none today; the mirror of job_review stays
    # exact).
    old_timer = CLEANUP_TIMERS.pop(jid, None)
    if old_timer:
        old_timer.cancel()
    job.pop("cleanup_at", None)
    job["status"] = "queued"
    _save(job)
    _enqueue_job(job, kind="captions2")
    return jsonify({"ok": True, "edited_count": changed})


@app.get("/api/captions/<jid>/srt")
def download_captions_srt(jid):
    """The Burmese .srt download: subs_my.srt — regenerated
    from the final edited segments when the render started,
    so it carries exactly the text that was burned, with the
    segments' original timings."""
    job = _load_job(jid)
    if not job or job.get("type") != "captions" \
            or not _owns(_current_user(), job):
        return jsonify({"error": "job မတွေ့ပါ"}), 404
    srt = JOBS_DIR / jid / "subs_my.srt"
    if not srt.exists():
        return jsonify({"error": "စာတန်း ဖိုင် မရှိသေးပါ"}), 404
    return send_file(srt, as_attachment=True,
                     download_name=f"myanmar-{jid}.srt",
                     conditional=True)


@app.get("/api/captions/<jid>/frame")
def captions_frame(jid):
    """Build 226: one frame of the parked job's OWN source,
    for the review-stage blur editor — the boxes are drawn on
    the real downloaded frame, exactly like the wizard's
    preview frames. Owner-scoped like every captions route;
    the grab is preview_frame's ffmpeg verbatim, and the
    source duration rides in a header for the editor's
    scrubber."""
    job = _load_job(jid)
    if not job or job.get("type") != "captions" \
            or not _owns(_current_user(), job):
        return jsonify({"error": "job မတွေ့ပါ"}), 404
    src = JOBS_DIR / jid / "source.mp4"
    if not src.exists():
        return jsonify({"error": "video မတွေ့ပါ"}), 404
    try:
        t = float(request.args.get("t", "2"))
    except ValueError:
        t = 2.0
    dur = probe_duration(src)
    t = min(max(t, 0.0), max(dur - 0.15, 0.0))
    frame = JOBS_DIR / jid / f"capframe_{t:.1f}.jpg"
    if not frame.exists():
        sh(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}",
            "-i", str(src), "-frames:v", "1", "-q:v", "3",
            str(frame)])
    resp = send_file(frame, mimetype="image/jpeg")
    resp.headers["X-Video-Duration"] = f"{dur:.2f}"
    return resp


# ---------- Download-only product (build 234) ----------
# Owner order 2026-11-20: a standalone service for customers
# who only want the FILE — link in, raw video out (watch in
# place, download, Telegram send through the ordinary job
# routes). NO transcription, NO translation, NO TTS, NO
# review park. It exists because Preview refuses videos over
# 60 minutes; this service has NO duration cap (the 2 GiB
# ceiling inside download() still applies). The record is a
# normal job (type "download"), so the queue, the jobs
# listing, the stream/download/Telegram routes, the ownership
# checks and the single 24h retention rule all serve it
# unchanged — the worker promotes the downloaded source.mp4
# to output.mp4, the name every one of those routes serves.
# Pricing (owner-set): DOWNLOAD_CREDITS_PER_MIN per STARTED
# minute (default 10 = 50 kyat/min), through
# _credits_for_minutes so the quote and the charge agree
# exactly. The duration is knowable only AFTER the download,
# so the admission quote runs inside the worker, right after
# download() returns: a non-exempt customer who cannot cover
# it fails there with the polite insufficient-credit message
# and is charged zero. Charge-on-success only: _finish is
# the single charge point, and the ledger records the charge
# under the distinct kind "download" (see _charge_for_job).

DOWNLOAD_STEPS = ["download", "save"]


def _download_view(job):
    view = {k: job.get(k) for k in
            ("id", "type", "url", "status", "current", "error",
             "quality", "duration_s", "output_size",
             "quote_minutes", "quote_credits", "charged_minutes",
             "created_at", "finished_at")}
    view["timings"] = _timings_of(job)  # build 213
    view["steps"] = job.get("steps") or []
    if job.get("status") == "queued":
        waiting = [q for q, _k in _JOB_QUEUE]
        if job["id"] in waiting:
            view["queue_pos"] = waiting.index(job["id"]) + 1
    return view


def _download_duration_s(workdir):
    """The downloaded video's duration in seconds: the
    resolver's own figure first (source_info.json, written by
    download()), ffprobe on the file when the resolver gave
    none. 0.0 when neither knows — the caller then bills the
    1-minute floor, exactly like _job_minutes' fallback."""
    try:
        info = json.loads(
            (Path(workdir) / "source_info.json").read_text(
                encoding="utf-8"))
        secs = float(info.get("duration") or 0)
        if secs > 0:
            return secs
    except Exception:
        pass
    try:
        return float(probe_duration(Path(workdir) / "source.mp4"))
    except Exception:
        return 0.0


def _download_minutes(seconds):
    """Started minutes for a measured duration (the house
    convention, same as _job_minutes): ceil(seconds / 60),
    minimum 1 — an unknown duration bills the floor."""
    if not seconds or seconds <= 0:
        return 1
    return max(1, math.ceil(seconds / 60.0))


def _download_worker(job):
    """The uninterrupted download-only run: download ->
    measure -> quote -> promote source.mp4 to output.mp4 ->
    _finish (the only place a download job is ever charged).
    The quote sits between the download and the promotion, so
    an insufficient balance fails the job BEFORE the file is
    made servable — and the failure is _fail's ordinary one
    (polite message, zero charged, 24h cleanup), never a
    charge. The "save" step is marked running before the
    quote so _fail does not remap the credit message through
    the download-error categories (its current == "download"
    branch is for download failures proper)."""
    workdir = JOBS_DIR / job["id"]
    threading.Thread(target=_heartbeat, args=(job,),
                     daemon=True).start()
    with job_context(job["id"]):
        try:
            job["status"] = "running"
            _timing_start(job)  # build 213
            _save(job)
            progress = _progress_fn(job)
            src = run_job.run_download(
                workdir, job["url"], progress=progress,
                quality=job.get("quality") or "hd")
            progress("save", "running")
            seconds = _download_duration_s(workdir)
            job["duration_s"] = round(seconds, 1) if seconds else None
            _quote_for_job(job, _download_minutes(seconds))
            src.replace(workdir / "output.mp4")
            progress("save", "done")
            _finish(job)
        except JobCancelled:
            _cancelled(job)
        except Exception as e:
            if job.get("cancel_requested"):
                _cancelled(job)
            else:
                _fail(job, e)


@app.post("/api/downloads")
def create_download():
    user = _current_user()
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "လင့်ခ် မမှန်ပါ"}), 400
    if not _public_url_ok(url):
        return jsonify({"error": "ဒီ လင့်ခ် ကို သုံးလို့ မရပါ"}), 400
    quality = data.get("quality") if data.get("quality") in \
        ("hd", "720", "480") else "hd"
    now = time.time()
    job = {"id": uuid.uuid4().hex[:12], "type": "download",
           "url": url, "mode": "download",
           "quality": quality,
           "source_path": None,
           "status": "queued", "current": None, "error": None,
           "created_at": now, "updated_at": now,
           "finished_at": None,
           "steps": [{"key": k, "label": STEP_LABELS[k],
                      "state": "pending"} for k in DOWNLOAD_STEPS]}
    if user:
        # Same admission gates as a captions job, in the same
        # order — both refuse before the job exists. (No
        # balance pre-check is possible here: the price needs
        # the downloaded duration, so the quote runs inside
        # the worker and fails the job there, zero charged.)
        setup_msg = _setup_gate_message(user)
        if setup_msg:
            return jsonify({"error": setup_msg}), 400
        job["user_id"] = user["id"]
        accounts.mark_has_job(user["id"])
        limit_msg = _daily_limit_message(user)
        if limit_msg:
            return jsonify({"error": limit_msg}), 400
    JOBS[job["id"]] = job
    _save(job)
    _enqueue_job(job, kind="download1")
    return jsonify(_download_view(job))


@app.get("/api/downloads/<jid>")
def get_download(jid):
    job = _load_job(jid)
    if not job or job.get("type") != "download" \
            or not _owns(_current_user(), job):
        return jsonify({"error": "job မတွေ့ပါ"}), 404
    return jsonify(_download_view(job))


# ---------- Title + hashtag helper (build 222, roadmap 6) -----
# On the DONE screen of a finished video job, one tap asks the
# SAME free-first engine chain that writes the Recap/Story/
# Commentary scripts (story._chain_generate — no new provider)
# for a ready-to-post TikTok/YouTube title + hashtags, written
# from the job's OWN text (its Burmese segments / script, plus
# the topic for a Story job). The result is cached on the job
# record (job["title_tags"]), so re-opening the done screen or
# re-tapping NEVER calls an engine again; only the explicit
# regenerate ({"force": true}) re-calls. FREE — no credit is
# charged, ever; a total engine failure is a polite Burmese
# error in the panel and the job itself is never touched.

TITLE_TAGS_PROMPT = """You write the posting text for a finished Burmese video. From the video text below, write:

1. ONE catchy title in Burmese for TikTok / YouTube — short, emotional, makes people want to tap. Personal, place and brand names stay in English letters (never Burmese script). One or two emoji are OK.
2. Then 6 to 8 hashtags on their own line — each starts with #, no spaces inside a hashtag, a sensible mix of Burmese and English (for example #Myanmar #မြန်မာ).

Answer with ONLY the title on the first line and the hashtags on the second line — no labels, no explanation, nothing else.

Video text:
__TEXT__
"""

TITLE_TAGS_FAIL = ("Title ထုတ်တာ မရပါ — AI service တွေ ခဏ "
                   "အလုပ်မလုပ်ပါ။ ခဏနေမှ ပြန်စမ်းပါ "
                   "(credit လုံးဝ မဖြတ်ပါ)")


def _title_tags_source(job):
    """The job's own words, excerpted (~1,600 chars): the
    Story topic first for a story job, then the script text
    (story / recap / commentary), then the Burmese segments
    every video product leaves in segments_my.json."""
    parts = []
    if job.get("story_topic"):
        parts.append(str(job["story_topic"]))
    for key in ("story_text", "recap_text", "commentary_text"):
        if job.get(key):
            parts.append(str(job[key]))
            break
    seg_path = JOBS_DIR / job["id"] / "segments_my.json"
    if seg_path.exists():
        try:
            from pipeline.common import load_segments
            segs = load_segments(seg_path)
            txt = " ".join((s.get("text_my") or s.get("text")
                            or "") for s in segs).strip()
            if txt:
                parts.append(txt)
        except Exception:
            pass
    return "\n".join(p for p in parts if p)[:1600]


def _parse_title_tags(text):
    """(extract for story._chain_generate) — defensively pull
    {"title", "hashtags"} out of a gateway reply: the title is
    the first non-empty line (a leading Title:/ခေါင်းစဉ် label,
    markdown bold and surrounding quotes stripped); hashtags
    are every #token anywhere in the reply, de-duplicated in
    order. Usable = a title plus at least 3 hashtags; anything
    less returns None so the chain fails over to the next
    gateway, exactly like the script writers' usability gate."""
    t = (text or "").strip()
    if not t:
        return None
    tags = []
    for tok in re.findall(r"#(\S+)", t):
        tok = tok.strip(".,!?'\"#)…")
        if tok and ("#" + tok) not in tags:
            tags.append("#" + tok)
    title = ""
    for line in t.splitlines():
        line = line.strip().strip("*").strip()
        line = re.sub(r"^(title|ခေါင်းစဉ်)\s*[:：]\s*", "",
                      line, flags=re.I).strip().strip('"').strip()
        if line and not line.startswith("#"):
            title = line
            break
    if not title or len(tags) < 3:
        return None
    return {"title": title[:200], "hashtags": tags[:10]}


@app.post("/api/jobs/<jid>/title-tags")
def job_title_tags(jid):
    """Free AI title + hashtags for a DONE job (owner only,
    same 404 shape as every other job route). Cached on the
    job record; {"force": true} regenerates explicitly."""
    job = _load_job(jid)
    if not job or not _owns(_current_user(), job):
        return jsonify({"error": "job မတွေ့ပါ"}), 404
    if job.get("status") != "done":
        return jsonify({"error": "ဗီဒီယို မပြီးသေးပါ — "
                        "ပြီးမှ Title ထုတ်နိုင်ပါမယ်"}), 409
    data = request.get_json(silent=True) or {}
    cached = job.get("title_tags")
    if cached and not data.get("force"):
        return jsonify({"title": cached.get("title") or "",
                        "hashtags": cached.get("hashtags") or [],
                        "cached": True})
    src = _title_tags_source(job)
    if not src:
        return jsonify({"error": "ဒီ job အတွက် စာသား မရှိလို့ "
                        "Title ထုတ်လို့ မရပါ"}), 409
    prompt = TITLE_TAGS_PROMPT.replace("__TEXT__", src)
    try:
        payload, provider = story_mod._chain_generate(
            prompt, _parse_title_tags)
    except Exception:
        return jsonify({"error": TITLE_TAGS_FAIL}), 502
    job["title_tags"] = {"title": payload["title"],
                         "hashtags": payload["hashtags"],
                         "provider": provider}
    _save(job)
    return jsonify({"title": payload["title"],
                    "hashtags": payload["hashtags"],
                    "cached": False})


# ---------- Cover maker (build 223, roadmap 7) ----------------
# On the DONE screen of a finished video job, one tap makes a
# clean TikTok cover: a sharp REAL frame from the customer's
# own finished video (the actual characters — never AI-faked
# imagery) plus a template overlay. The step ASKS the creator
# for the title text and the part number (owner's design,
# 2026-11-17); the series name is remembered per account
# (accounts.cover_series) so the next part of a series only
# needs the number bumped. FREE — no credit moves anywhere in
# this flow, and the job's own files are never touched: frames
# and the finished cover live in the job's cover/ subdir and
# are swept away with the job by the 24 h cleanup.

COVER_FRAME_FRACS = (0.08, 0.28, 0.48, 0.68, 0.88)
_COVER_NAME_RE = re.compile(
    r"^(frame|thumb)_\d+\.jpg$|^cover\.jpg$")


def _cover_dir(job):
    return JOBS_DIR / job["id"] / "cover"


def _cover_frames(job):
    """Extract ~5 candidate frames from the job's output.mp4 at
    spread timestamps (re-extraction replaces the old set).
    Returns [{"i", "t", "thumb", "frame"}] or None when the job
    has no readable output video."""
    out = JOBS_DIR / job["id"] / "output.mp4"
    if not out.exists():
        return None
    dur = probe_duration(out)
    if not dur or dur <= 0:
        return None
    cdir = _cover_dir(job)
    cdir.mkdir(parents=True, exist_ok=True)
    frames = []
    for i, frac in enumerate(COVER_FRAME_FRACS):
        t = min(max(dur * frac, 0.0), max(dur - 0.15, 0.0))
        frame = cdir / f"frame_{i}.jpg"
        thumb = cdir / f"thumb_{i}.jpg"
        try:
            sh(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}",
                "-i", str(out), "-frames:v", "1", "-q:v", "2",
                str(frame)])
            sh(["ffmpeg", "-y", "-v", "error", "-i", str(frame),
                "-vf", "scale=270:-2", "-q:v", "4", str(thumb)])
        except Exception:
            continue
        if frame.exists() and thumb.exists():
            frames.append({
                "i": i, "t": round(t, 1),
                "thumb": f"/api/jobs/{job['id']}/cover/"
                         f"thumb_{i}.jpg",
                "frame": f"/api/jobs/{job['id']}/cover/"
                         f"frame_{i}.jpg"})
    return frames or None


def _cover_font(size, bold=True):
    from PIL import ImageFont
    from pipeline.common import FONTS_DIR, ensure_fonts
    ensure_fonts()
    name = ("NotoSansMyanmar-Bold.ttf" if bold
            else "NotoSansMyanmar-Regular.ttf")
    return ImageFont.truetype(str(FONTS_DIR / name),
                              max(10, int(size)))


def _cover_wrap(draw, text, font, max_w):
    """Word-wrap `text` into lines of at most max_w px, measured
    with the real font; a single word wider than the line is
    split by characters so nothing ever spills off the cover."""
    def fits(s):
        return draw.textlength(s, font=font) <= max_w
    lines, cur = [], ""
    for word in (text or "").split():
        trial = (cur + " " + word).strip()
        if fits(trial):
            cur = trial
            continue
        if cur:
            lines.append(cur)
            cur = ""
        while not fits(word) and len(word) > 1:
            cut = len(word)
            while cut > 1 and not fits(word[:cut]):
                cut -= 1
            lines.append(word[:cut])
            word = word[cut:]
        cur = word
    if cur:
        lines.append(cur)
    return lines


def _compose_cover(frame_path, title, series, part, out_path):
    """The template over the REAL frame (PIL, bundled Myanmar
    fonts): native aspect (width capped at 1080), a dark
    gradient band across the bottom third carrying the title
    (largest size that fits ≤3 lines), a red "PART <n>" badge
    at the top-right when a part is given, and the series name
    small at the top-left when given."""
    from PIL import Image, ImageDraw
    img = Image.open(frame_path).convert("RGB")
    if img.width > 1080:
        img = img.resize(
            (1080, round(img.height * 1080 / img.width)),
            Image.LANCZOS)
    w, h = img.size
    margin = max(14, round(w * 0.05))
    # Dark gradient band across the bottom ~38%: transparent at
    # its top edge, near-opaque black at the bottom.
    band_h = round(h * 0.38)
    band_top = h - band_h
    grad = Image.new("L", (1, band_h))
    for yy in range(band_h):
        grad.putpixel((0, yy),
                      int(228 * (yy / max(band_h - 1, 1)) ** 1.3))
    img.paste(Image.new("RGB", (w, band_h), (0, 0, 0)),
              (0, band_top), grad.resize((w, band_h)))
    draw = ImageDraw.Draw(img, "RGBA")
    # Title — shrink from ~8% of the width until the wrap fits
    # ≤3 lines inside the band.
    avail_w = w - 2 * margin
    size = max(16, round(w * 0.082))
    lines, font = [], _cover_font(size)
    while True:
        font = _cover_font(size)
        lines = _cover_wrap(draw, title, font, avail_w)
        if len(lines) <= 3 \
                and len(lines) * size * 1.45 <= band_h - margin:
            break
        if size <= 12:
            lines = lines[:3]
            break
        size = max(12, round(size * 0.88))
    line_h = size * 1.45
    y = band_top + (band_h - len(lines) * line_h) / 2
    for ln in lines:
        x = (w - draw.textlength(ln, font=font)) / 2
        draw.text((x + 2, y + 2), ln, font=font,
                  fill=(0, 0, 0, 210))
        draw.text((x, y), ln, font=font,
                  fill=(255, 255, 255, 255))
        y += line_h
    # Series name — small chip at the top-left.
    if series:
        sfont = _cover_font(max(13, round(w * 0.034)))
        sw = draw.textlength(series, font=sfont)
        pad = round(w * 0.018) + 4
        draw.rounded_rectangle(
            [margin, margin, margin + sw + 2 * pad,
             margin + sfont.size * 1.9],
            radius=8, fill=(0, 0, 0, 150))
        draw.text((margin + pad, margin + sfont.size * 0.32),
                  series, font=sfont, fill=(255, 255, 255, 255))
    # Part badge — red rounded chip at the top-right.
    if part:
        label = f"PART {part}"
        bfont = _cover_font(max(14, round(w * 0.042)))
        bw = draw.textlength(label, font=bfont)
        pad = round(w * 0.022) + 4
        x1 = w - margin
        draw.rounded_rectangle(
            [x1 - bw - 2 * pad, margin, x1,
             margin + bfont.size * 1.9],
            radius=9, fill=(228, 18, 38, 255))
        draw.text((x1 - pad - bw, margin + bfont.size * 0.32),
                  label, font=bfont, fill=(255, 255, 255, 255))
    img.save(out_path, "JPEG", quality=88)
    return out_path


@app.post("/api/jobs/<jid>/cover/frames")
def job_cover_frames(jid):
    """Extract the candidate cover frames for a DONE job
    (owner only, same 404 shape as every other job route)."""
    job = _load_job(jid)
    if not job or not _owns(_current_user(), job):
        return jsonify({"error": "job မတွေ့ပါ"}), 404
    if job.get("status") != "done":
        return jsonify({"error": "ဗီဒီယို မပြီးသေးပါ — "
                        "ပြီးမှ Cover လုပ်နိုင်ပါမယ်"}), 409
    frames = _cover_frames(job)
    if not frames:
        return jsonify({"error": "ဒီ ဗီဒီယိုကနေ ပုံ ထုတ်လို့ "
                        "မရပါ"}), 400
    return jsonify({"frames": frames})


@app.get("/api/cover/series")
def cover_series():
    """The signed-in account's remembered cover series (name +
    highest part made), most recent first — the cover form
    offers them as chips and suggests last part + 1."""
    user = _current_user()
    if not user:
        return jsonify({"error": "အရင် login ဝင်ပါ"}), 401
    return jsonify(
        {"series": accounts.cover_series_list(user["id"])})


@app.post("/api/jobs/<jid>/cover")
def job_cover_make(jid):
    """Compose the cover for a DONE job from a chosen extracted
    frame + the creator's title / series / part. FREE. When a
    series is named, the account's series record is upserted
    with this part (last part only ever moves up)."""
    job = _load_job(jid)
    user = _current_user()
    if not job or not _owns(user, job):
        return jsonify({"error": "job မတွေ့ပါ"}), 404
    if job.get("status") != "done":
        return jsonify({"error": "ဗီဒီယို မပြီးသေးပါ — "
                        "ပြီးမှ Cover လုပ်နိုင်ပါမယ်"}), 409
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "Cover အတွက် ခေါင်းစဉ် စာသား "
                        "အရင် ထည့်ပါ"}), 400
    try:
        idx = int(data.get("frame"))
    except (TypeError, ValueError):
        return jsonify({"error": "ပုံ အရင် ရွေးပါ"}), 400
    cdir = _cover_dir(job)
    frame = cdir / f"frame_{idx}.jpg"
    if not frame.exists():
        _cover_frames(job)  # not extracted yet — do it now
    if not frame.exists():
        return jsonify({"error": "ရွေးထားတဲ့ ပုံ မတွေ့ပါ — "
                        "ပုံ ပြန်ရွေးပါ"}), 400
    series = (data.get("series") or "").strip()
    part = data.get("part")
    try:
        part = int(part) if part not in (None, "") else None
    except (TypeError, ValueError):
        part = None
    if part is not None and part <= 0:
        part = None
    try:
        _compose_cover(frame, title, series, part,
                       cdir / "cover.jpg")
    except Exception:
        return jsonify({"error": "Cover ထုတ်တာ မရပါ — "
                        "ခဏနေမှ ပြန်စမ်းပါ"}), 502
    saved = None
    if series and user:
        saved = accounts.cover_series_upsert(
            user["id"], series, part)
    job["cover"] = {"title": title, "series": series or None,
                    "part": part, "frame": idx}
    _save(job)
    return jsonify({"url": f"/api/jobs/{jid}/cover/cover.jpg",
                    "series": saved})


@app.get("/api/jobs/<jid>/cover/<name>")
def job_cover_file(jid, name):
    """Serve one file from the job's cover/ dir (candidate
    frames, thumbnails, the finished cover) — owner only, and
    the name is whitelisted so nothing else can be reached."""
    job = _load_job(jid)
    if not job or not _owns(_current_user(), job):
        return jsonify({"error": "job မတွေ့ပါ"}), 404
    if not _COVER_NAME_RE.match(name or ""):
        return jsonify({"error": "ဖိုင် မတွေ့ပါ"}), 404
    p = _cover_dir(job) / name
    if not p.exists():
        return jsonify({"error": "ဖိုင် မတွေ့ပါ"}), 404
    return send_file(p, mimetype="image/jpeg")


# ---------- Reseller API (build 224, roadmap 8) ----------
# A token-keyed API so shops can build on this engine (Burma
# Voice has no API at all). Auth is a per-account Bearer token
# (accounts.set_api_token — only its SHA-256 hash is stored,
# the plaintext is shown exactly once in the profile) and it
# is FULLY separate from web sessions: _api_user() reads the
# Authorization header and nothing else, web routes read the
# session cookie and nothing else, and _accounts_guard exempts
# /api/v1/ so neither path can substitute for the other.
#
# v1 surface: balance, job submit (dub + captions, URL only —
# no uploads, and no Recap/Story/Commentary: those need the
# interactive script park), job status/list, and downloads via
# DEDICATED token-authenticated routes (the session-gated web
# file routes stay session-only). API jobs are ordinary jobs:
# the same queue, the same admission gates (setup, daily cap),
# the same ElevenLabs test-gate (through _new_job), the same
# quote-after-phase-1 and the same charge-on-success — an API
# dub runs the shared _worker; an API captions job runs
# _api_captions_worker, the uninterrupted form of the web
# captions pair (no review park: phase 1 -> quote -> burn).


def _api_user():
    """The account behind the request's Bearer token, or None.
    THE auth check for every /api/v1 route — a session cookie
    is never even looked at here."""
    auth = request.headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        return None
    return accounts.user_for_api_token(auth[len("Bearer "):])


def _api_auth_error():
    return jsonify({"error": "invalid or missing API token"}), 401


def _api_job_view(job):
    """The API shape of a job: product mode in API terms
    ("dub" | "captions"), lifecycle status, current stage,
    the success charge, and — once done — the token download
    URLs (paths; clients resolve them against the host)."""
    jid = job["id"]
    view = {"id": jid,
            "mode": job.get("api_mode")
            or ("captions" if job.get("type") == "captions"
                else "dub"),
            "status": job.get("status"),
            "stage": job.get("current"),
            "error": job.get("error"),
            "charged": job.get("charged_minutes") or 0,
            "created_at": job.get("created_at"),
            "finished_at": job.get("finished_at")}
    if job.get("status") == "queued":
        waiting = [q for q, _k in _JOB_QUEUE]
        if jid in waiting:
            view["queue_pos"] = waiting.index(jid) + 1
    if job.get("status") == "done":
        view["download_url"] = f"/api/v1/jobs/{jid}/download"
        if job.get("type") == "captions":
            view["srt_url"] = f"/api/v1/jobs/{jid}/srt"
    return view


def _api_captions_worker(job):
    """Uninterrupted captions run for the API: phase 1 (the
    shared guarded subs chain) -> quote -> regenerate
    subs_my.srt from the final segments -> burn into the
    original video -> _finish (the only charge point). The
    web pair (_captions_worker + _captions_worker2) parks at
    waiting_review between the same two halves for the human
    edit; an API client has no review step, so the halves run
    back to back here with identical pieces."""
    workdir = JOBS_DIR / job["id"]
    threading.Thread(target=_heartbeat, args=(job,),
                     daemon=True).start()
    with job_context(job["id"]):
        try:
            job["status"] = "running"
            _timing_start(job)  # build 213
            _save(job)
            run_job.run_phase1(
                workdir, job["url"],
                source_path=job.get("source_path"),
                progress=_progress_fn(job), style="subs",
                quality=job.get("quality") or "hd")
            _quote_for_job(job)
            from pipeline.common import load_segments, write_srt
            segs = load_segments(workdir / "segments_my.json")
            if not segs:
                raise RuntimeError(
                    "စာတန်း မရှိပါ — ပြန်စမ်းပါ "
                    "(credit လုံးဝ မဖြတ်ပါ)")
            write_srt(segs, workdir / "subs_my.srt")
            run_job.run_phase2(
                workdir, mode="subs", progress=_progress_fn(job),
                font=job.get("font") or "noto", subs=None,
                fx=job.get("fx"),
                quality=job.get("quality") or "hd",
                karaoke=bool(job.get("karaoke")))
            _finish(job)
        except JobCancelled:
            _cancelled(job)
        except Exception as e:
            if job.get("cancel_requested"):
                _cancelled(job)
            else:
                _fail(job, e)


# ----- token management (web session routes) -----

@app.get("/api/api-token")
def api_token_status():
    """Profile API block: whether a token exists. The token
    itself is NEVER returned — not here, not anywhere."""
    user = _current_user()
    if not user:
        return jsonify({"error": "အရင် login ဝင်ပါ"}), 401
    return jsonify({"has_token":
                    accounts.has_api_token(user["id"])})


@app.post("/api/api-token")
def api_token_generate():
    """Generate (or regenerate) the account's API token. The
    plaintext rides THIS response only — the one time it is
    ever shown; regenerating kills the previous token in the
    same write."""
    user = _current_user()
    if not user:
        return jsonify({"error": "အရင် login ဝင်ပါ"}), 401
    return jsonify({"token": accounts.set_api_token(user["id"])})


@app.delete("/api/api-token")
def api_token_revoke():
    user = _current_user()
    if not user:
        return jsonify({"error": "အရင် login ဝင်ပါ"}), 401
    accounts.revoke_api_token(user["id"])
    return jsonify({"ok": True, "has_token": False})


# ----- v1 routes (Bearer token auth only) -----

@app.get("/api/v1/balance")
def api_v1_balance():
    user = _api_user()
    if not user:
        return _api_auth_error()
    return jsonify({"username": user["username"],
                    "credits": user["credits"] or 0})


@app.post("/api/v1/jobs")
def api_v1_create_job():
    user = _api_user()
    if not user:
        return _api_auth_error()
    data = request.get_json(force=True, silent=True) or {}
    mode = str(data.get("mode") or "").strip().lower()
    if mode not in ("dub", "captions"):
        return jsonify({"error":
                        "mode must be 'dub' or 'captions'"}), 400
    url = (data.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "လင့်ခ် မမှန်ပါ"}), 400
    if not _public_url_ok(url):
        return jsonify({"error": "ဒီ လင့်ခ် ကို သုံးလို့ မရပါ"}), 400
    # Build 226: optional blur_boxes — the same normalized
    # boxes the web editor produces ({x, y, w, h} as frame
    # fractions, optional strength). Strict like every v1
    # field: a bad shape is a 400, never a silent drop; the
    # boxes then normalize through the web path (_new_job for
    # dub, _fx_from_data for captions) and reach the render
    # exactly as a web customer's do. Charges unchanged.
    boxes_in = data.get("blur_boxes")
    if boxes_in is not None:
        if not isinstance(boxes_in, list):
            return jsonify({"error": "blur_boxes မမှန်ပါ"}), 400
        if len(boxes_in) > fx_mod.MAX_BLUR_BOXES:
            return jsonify({"error":
                            "ဝါးမည့် အကွက် အများဆုံး ၅ ခု ပဲ "
                            "ရပါတယ်"}), 400
        for b in boxes_in:
            if not isinstance(b, dict):
                return jsonify({"error": "blur_boxes မမှန်ပါ"}), 400
            try:
                _vals = [float(b[k]) for k in ("x", "y", "w", "h")]
                if "strength" in b:
                    float(b["strength"])
            except (KeyError, TypeError, ValueError):
                return jsonify({"error": "blur_boxes မမှန်ပါ"}), 400
            if _vals[2] <= 0 or _vals[3] <= 0:
                return jsonify({"error": "blur_boxes မမှန်ပါ"}), 400
    api_fx = {"fx": {"blur_boxes": boxes_in}} if boxes_in else {}
    if mode == "dub":
        voice = data.get("voice")
        if voice is not None:
            voice = str(voice).strip()
            # A voice the API does not know is a 400, never a
            # silent fallback: the reseller must learn their
            # request was wrong. Style keys and "el:<id>" picks
            # are the vocabulary; _new_job then applies the
            # build-206 gate to a premium pick exactly as web.
            if voice and not elevenlabs_tts.is_el_voice(voice) \
                    and voice not in tts.STYLE_BY_KEY:
                return jsonify({"error": "voice မမှန်ပါ"}), 400
        _kar = data.get("karaoke")
        _api_karaoke = _kar is True or (
            isinstance(_kar, str)
            and _kar.strip().lower()
            in ("1", "true", "on", "yes"))
        job, err = _new_job({"url": url, "mode": "both",
                             "voice": voice or None,
                             "karaoke": _api_karaoke,
                             **api_fx}, user=user)
        if err:
            return jsonify({"error": err}), 400
        job["api_mode"] = "dub"
        kind = "main"
    else:
        _kar = data.get("karaoke")
        karaoke = _kar is True or (isinstance(_kar, str)
                                   and _kar.strip().lower()
                                   in ("1", "true", "on", "yes"))
        api_job_fx, _api_fx_err = _fx_from_data(api_fx)
        now = time.time()
        job = {"id": uuid.uuid4().hex[:12], "type": "captions",
               "url": url, "mode": "captions",
               "quality": "hd", "font": "noto", "fx": api_job_fx,
               "karaoke": karaoke, "api_mode": "captions",
               "source_path": None,
               "status": "queued", "current": None, "error": None,
               "created_at": now, "updated_at": now,
               "finished_at": None,
               "steps": [{"key": k, "label": STEP_LABELS[k],
                          "state": "pending"}
                         for k in CAPTIONS_STEPS]}
        kind = "captions_api"
    # The admission gates every web submission passes, in the
    # same spirit: setup first, then the daily cap, then the
    # balance. Every refusal lands BEFORE the job exists.
    setup_msg = _setup_gate_message(user)
    if setup_msg:
        return jsonify({"error": setup_msg}), 400
    limit_msg = _daily_limit_message(user)
    if limit_msg:
        return jsonify({"error": limit_msg}), 429
    if user["role"] != "admin" and (user["credits"] or 0) < 30:
        # The cheapest any successful job can be is 30 credits
        # (the captions minimum, build 231) — below that, no
        # product can ever be afforded, so refuse up front like
        # the web pre-check. Fuller quotes still happen after
        # phase 1 (_quote_for_job), exactly as web, once the
        # duration is known.
        return jsonify({"error": "credit မလောက်ပါ",
                        "balance": user["credits"] or 0,
                        "needed": 30}), 402
    job["user_id"] = user["id"]
    job["via"] = "api"
    accounts.mark_has_job(user["id"])
    JOBS[job["id"]] = job
    _save(job)
    _enqueue_job(job, kind=kind)
    return jsonify({"job_id": job["id"], "status": job["status"]}), 201


@app.get("/api/v1/jobs")
def api_v1_list_jobs():
    user = _api_user()
    if not user:
        return _api_auth_error()
    mine = [j for j in _all_jobs()
            if j.get("user_id") == user["id"]]
    return jsonify({"jobs": [_api_job_view(j) for j in mine[:20]]})


@app.get("/api/v1/jobs/<jid>")
def api_v1_get_job(jid):
    user = _api_user()
    if not user:
        return _api_auth_error()
    job = _load_job(jid)
    if not job or job.get("user_id") != user["id"]:
        return jsonify({"error": "job မတွေ့ပါ"}), 404
    return jsonify(_api_job_view(job))


@app.get("/api/v1/jobs/<jid>/download")
def api_v1_download(jid):
    """The finished video, token-authenticated (the web
    download route stays session-only). Owner-only, done-only;
    conditional responses keep Range/resume working."""
    user = _api_user()
    if not user:
        return _api_auth_error()
    job = _load_job(jid)
    if not job or job.get("user_id") != user["id"]:
        return jsonify({"error": "job မတွေ့ပါ"}), 404
    out = JOBS_DIR / jid / "output.mp4"
    if job.get("status") != "done" or not out.exists():
        return jsonify({"error": "မပြီးသေးပါ"}), 404
    return send_file(out, as_attachment=True,
                     download_name=f"myanmar-{jid}.mp4",
                     conditional=True)


@app.get("/api/v1/jobs/<jid>/srt")
def api_v1_srt(jid):
    """The Burmese .srt of a captions job (subs_my.srt — the
    same file the web download serves)."""
    user = _api_user()
    if not user:
        return _api_auth_error()
    job = _load_job(jid)
    if not job or job.get("user_id") != user["id"] \
            or job.get("type") != "captions":
        return jsonify({"error": "job မတွေ့ပါ"}), 404
    srt = JOBS_DIR / jid / "subs_my.srt"
    if job.get("status") != "done" or not srt.exists():
        return jsonify({"error": "စာတန်း ဖိုင် မရှိသေးပါ"}), 404
    return send_file(srt, as_attachment=True,
                     download_name=f"myanmar-{jid}.srt",
                     conditional=True)


# ---------- text-only export + bulk text input ----------
# Both read/write the SAME store the review API uses (segments_my.json),
# so exports always reflect the user's saved edits, and bulk-pasted text
# is exactly what phase 2 dubs (TTS) and burns (subtitles) in mode "both".

def _segments_or_error(jid):
    """(job, segments, error_response) — segments None when error is set."""
    job = _load_job(jid)
    if not job or not _owns(_current_user(), job):
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
    if not job or not _owns(_current_user(), job):
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
    """Remove leftovers from before a restart — honouring the
    single 24h retention window (build 215; the old flat 1-hour
    sweep broke the 24h promise on every restart). Queued/running
    leftovers are left for the heal in _scan_disk_jobs (build 204)."""
    now = time.time()
    # Build 203 (audit V08): preview dirs an active job still
    # references survive the sweep, exactly as in _cleanup_job.
    keep = _active_preview_dirs()
    for d in list(JOBS_DIR.iterdir()):
        if not d.is_dir():
            continue
        meta = None
        try:
            meta = json.loads((d / "job.json").read_text(encoding="utf-8"))
        except Exception:
            meta = None
        if meta and meta.get("status") in ("done", "error",
                                           "cancelled"):
            ref = meta.get("finished_at") or meta.get("created_at") or 0
            if now - float(ref) > _job_retention(meta):
                shutil.rmtree(d, ignore_errors=True)
                url = meta.get("url")
                if url:
                    for _pid, pdir in _preview_dirs_for(url):
                        if str(pdir) not in keep:
                            shutil.rmtree(pdir, ignore_errors=True)
            continue
        if meta:
            continue
        try:
            if now - d.stat().st_mtime > 3600:
                shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass
    for d in list(PREVIEWS_DIR.iterdir()):
        try:
            if d.is_dir() and str(d) not in keep \
                    and now - d.stat().st_mtime > 3600:
                shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


try:
    _startup_sweep()
except Exception:
    pass

try:
    # Build 204 (audit V11): the one startup recovery pass over
    # disk jobs (heal / re-arm / expire) that _all_jobs used to
    # redo on every list request.
    _scan_disk_jobs()
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
for _target in (_backup_loop, _job_dispatcher, _upload_sweeper):
    try:
        threading.Thread(target=_target, daemon=True).start()
    except Exception:
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8611"))
    # Build 193: serve through waitress (production WSGI server),
    # matching deploy/serve.py; fall back to the Werkzeug dev
    # server only where waitress is not installed (bare dev runs).
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=port, threads=8)
    except ImportError:
        app.run(host="0.0.0.0", port=port, threaded=True)
