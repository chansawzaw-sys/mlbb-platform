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
  * Build 184: every Bot API call goes through api_base(). By default
    that is the cloud API above, but when the owner saves a Telegram
    api_id/api_hash (my.telegram.org) the deploy bootstrap installs a
    self-hosted telegram-bot-api server on this same VPS and points
    TG_API_BASE at it (127.0.0.1:8081). A local server lifts sends to
    2 GB and returns absolute filesystem paths from getFile (its
    --local mode), which download_file() handles below.

Recipients persist in .tg_recipients.json in the app dir (survives
app updates like .env does; never in git).
"""
import fcntl
import json
import os
import re
import shutil
import threading
import time
import traceback
from pathlib import Path

_LOCK = threading.Lock()
_ME_CACHE = {}
_POLLER_STARTED = False

WELCOME = ("မင်္ဂလာပါ 🙏 မှတ်ပုံတင် ပြီးပါပြီ ✅\n"
           "🎬 ဘာသာပြန် လိုတဲ့ video link (YouTube / TikTok) ကို "
           "ဒီမှာ ပို့ပေးပါ — ပြီးတာနဲ့ ဗီဒီယို ကို ဒီ chat ထဲမှာပဲ "
           "ပြန်ပို့ ပေးပါမယ်\n"
           "🔗 App: https://85-155-149-58.sslip.io\n"
           "📲 အဲဒီ link ကို browser မှာ ဖွင့်ပြီး menu (⋮) → "
           "«Add to Home screen» နှိပ်ရင် app အဖြစ် ထည့်ထားနိုင်ပါတယ်\n"
           "အောက်က Menu ခလုတ်တွေ နဲ့လည်း သုံးနိုင်ပါတယ် 👇")

# Diagnostics for the settings card: when the poller last completed
# a poll and what it last choked on (if anything).
LAST_POLL = {"at": 0.0, "error": ""}

# Build 245 (bot went deaf 2026-11-21): queue-regression self-heal
# state. _EMPTY_POLLS counts CONSECUTIVE successful getUpdates
# passes that returned zero updates; at _EMPTY_POLL_LIMIT the
# poller reconciles its persisted offset/seen_max against the
# server's actual newest update (see _reconcile_queue_inner).
# _LAST_SKIP_LOG throttles the all-skipped-batch log line.
_EMPTY_POLLS = 0
_EMPTY_POLL_LIMIT = 20
_LAST_SKIP_LOG = 0.0

# Build 246: reconciliation observability for the app watchdog —
# every ACTUAL queue reset bumps this counter, so the watchdog
# can tell the owner a self-heal happened (it is otherwise
# invisible until a user complains).
RECONCILE_STATE = {"count": 0, "last_at": 0.0}


def reconcile_status():
    return dict(RECONCILE_STATE)


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


def _env_get(key):
    """One value from the process env, else from the app .env —
    read live on every call so admin edits apply without a restart
    (the same rule token() has always followed)."""
    v = (os.environ.get(key) or "").strip()
    if v:
        return v
    p = _env_path()
    if p.exists():
        for ln in p.read_text(encoding="utf-8").splitlines():
            if ln.startswith(key + "="):
                return ln.split("=", 1)[1].strip()
    return ""


def token():
    """Bot token from the environment or the app .env, or None."""
    return _env_get("TELEGRAM_BOT_TOKEN") or None


# ---------------- Bot API base (build 184) ----------------
# TG_API_BASE is written into .env ONLY by the deploy bootstrap,
# and only when a local telegram-bot-api server is actually running
# (credentials present + binary installed + unit active). Unset or
# blank always means the cloud API.

CLOUD_BASE = "https://api.telegram.org"


def api_base():
    return _env_get("TG_API_BASE").rstrip("/") or CLOUD_BASE


def is_local():
    return api_base() != CLOUD_BASE


def api_mode():
    return "local" if is_local() else "cloud"


def send_cap():
    """Largest file the bot may send in the current mode."""
    return LOCAL_MAX_BYTES if is_local() else MAX_BYTES


def download_cap():
    """Largest user file the bot may download in the current mode
    (the cloud getFile endpoint refuses anything over 20 MB; a
    local server has no such cap)."""
    return LOCAL_MAX_BYTES if is_local() else MAX_DOWNLOAD_BYTES


def _api_url(tok, method):
    return f"{api_base()}/bot{tok}/{method}"


def valid_format(tok):
    return bool(tok) and bool(
        re.fullmatch(r"\d{6,12}:[A-Za-z0-9_-]{30,}", tok.strip()))


def _call(tok, method, timeout=30, **params):
    import requests
    r = requests.post(_api_url(tok, method),
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


def _poll_lock_path():
    return _root() / ".tg_poll.lock"


def poll_once():
    """One getUpdates pass: register anyone who talked to the bot and
    greet first-timers. Returns how many updates were consumed.

    Build 199 (bot spam bursts 2026-11-15, twice in one day): two
    defences wrap the pass. (1) A cross-process flock serialises
    poll cycles — during a restart overlap two app processes could
    otherwise work the same backlog and dispatch every update
    twice; while the lock is held this call is a no-op. (2) A
    persisted dispatch watermark (store["seen_max"], in
    _poll_once_inner) means an already-dispatched update is never
    dispatched again, even if Telegram re-delivers it — e.g. the
    API endpoint flipping between the local Bot API server and the
    cloud across a restart."""
    try:
        lf = open(_poll_lock_path(), "a+")
    except Exception:
        lf = None  # lock file unusable: poll anyway rather than never
    if lf is not None:
        try:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lf.close()
            return 0  # another poller is mid-cycle; skip this pass
    try:
        return _poll_once_inner()
    finally:
        if lf is not None:
            try:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            lf.close()


def _poll_once_inner():
    global _EMPTY_POLLS, _LAST_SKIP_LOG
    tok = token()
    if not tok:
        return 0
    import requests
    with _LOCK:
        store = _load_store()
    r = requests.get(_api_url(tok, "getUpdates"),
                     params={"offset": store.get("offset", 0),
                             "timeout": 30,
                             "allowed_updates": '["message", "callback_query"]'},
                     timeout=40)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(
            f"telegram getUpdates: {data.get('description')}")
    LAST_POLL["at"] = time.time()
    LAST_POLL["error"] = ""
    updates = data.get("result") or []
    # Build 245: a sustained run of successful-but-empty polls is
    # the signature of our persisted offset having jumped past the
    # server's queue (the queue regressed — e.g. the local Bot API
    # server restarted and renumbered its updates). Reconcile
    # against the server's newest update; a healthy queue makes
    # this a strict no-op. Runs under poll_once's flock.
    if updates:
        _EMPTY_POLLS = 0
    else:
        _EMPTY_POLLS += 1
        if _EMPTY_POLLS >= _EMPTY_POLL_LIMIT:
            _EMPTY_POLLS = 0
            _reconcile_queue_inner()
    seen_max = store.get("seen_max", 0)
    fresh = []
    new_users = []
    start_chats = set()
    for u in updates:
        # The offset advances past EVERY update, re-delivered or
        # not, so Telegram stops offering it...
        store["offset"] = max(store.get("offset", 0), u["update_id"] + 1)
        if u["update_id"] <= seen_max:
            # ...but only never-dispatched updates are collected.
            # The watermark is written with the store save below,
            # BEFORE any greeting or handler runs
            # (mark-before-dispatch), so even a crash mid-dispatch
            # can never re-dispatch them.
            continue
        seen_max = u["update_id"]
        fresh.append(u)
        _t = (u.get("message") or {}).get("text") or ""
        if _t.startswith("/start"):
            _c = (u.get("message") or {}).get("chat") or {}
            if _c.get("id"):
                start_chats.add(str(_c["id"]))
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
    store["seen_max"] = seen_max
    if updates:
        with _LOCK:
            _save_store(store)
    if updates and not fresh:
        # Build 245: the server returned updates but the seen_max
        # watermark swallowed EVERY one — the exact signature of
        # the 2026-11-21 deaf bot. Log it (throttled to one line
        # a minute) so the journal shows the swallow happening
        # instead of the bot eating updates in silence.
        _now = time.time()
        if _now - _LAST_SKIP_LOG >= 60:
            _LAST_SKIP_LOG = _now
            print(f"TG POLL skipped {len(updates)} update(s): "
                  f"all <= seen_max {seen_max}", flush=True)
    for cid in new_users:
        if MESSAGE_HANDLER and cid in start_chats:
            continue  # the handler answers /start itself (with menu)
        try:
            _call(tok, "sendMessage", chat_id=cid, text=WELCOME)
        except Exception:
            pass  # registration stands even if the greeting fails
    if MESSAGE_HANDLER:
        for u in fresh:
            msg = u.get("message")
            if msg:
                _dispatch(MESSAGE_HANDLER, msg)
    if CALLBACK_HANDLER:
        for u in fresh:
            cq = u.get("callback_query")
            if cq:
                _dispatch(CALLBACK_HANDLER, cq)
    return len(updates)


# ---------------- Build 245: queue-regression self-heal --------
# The 2026-11-21 deaf bot: polls returned ok+empty forever while
# users' messages never dispatched. The persisted offset/seen_max
# live in the APP's id space, but the server queue (the LOCAL Bot
# API server especially — it can restart/reset, plausibly OOMing
# on a ~900 MB file) has its OWN update-id sequence. If that
# sequence ever regresses below our stored offset, every real
# update is either never returned (our offset points past the
# whole queue) or skipped as "already dispatched"
# (update_id <= seen_max) — permanently, with no error anywhere.
# Reconciliation peeks at the server's newest update and, only
# when our offset provably points past it, rewinds the store so
# normal polling can see the queue again.

def _probe_latest_update_id(tok):
    """The server's newest update id (getUpdates offset=-1), or
    None when the queue is totally empty or the probe fails. A
    failed probe is evidence of NOTHING and must never cause a
    state change."""
    import requests
    try:
        r = requests.get(_api_url(tok, "getUpdates"),
                         params={"offset": -1, "limit": 1},
                         timeout=10)
        data = r.json()
    except Exception:
        return None
    if not data.get("ok"):
        return None
    res = data.get("result") or []
    if not res:
        return None
    return max(u["update_id"] for u in res)


def _reconcile_queue_inner():
    """Reset the store if — and only if — the queue regressed.
    The caller holds poll_once's flock (or is reconcile_queue,
    which takes it). Returns True when a reset happened.

    Healthy test: our stored offset can still see the server's
    newest update (offset <= latest + 1). If offset points PAST
    the newest update, a monotonic queue could never have got
    there — the server's id sequence must have restarted below
    us. The reset rewinds offset AND seen_max to 0 (preserving
    users and every other store key) so the next normal poll
    re-fetches whatever the server still holds and dispatches
    it exactly once through the usual watermark path; the store
    then re-advances to (latest + 1, latest) on its own. Simply
    parking the counters AT latest + 1 / latest instead would
    silently discard any backlog the server still holds —
    tonight those were real user messages."""
    tok = token()
    if not tok:
        return False
    with _LOCK:
        store = _load_store()
    latest = _probe_latest_update_id(tok)
    if latest is None:
        return False  # empty queue or failed probe: no conclusion
    offset = store.get("offset", 0)
    if offset <= latest + 1:
        return False  # healthy: the newest update is reachable
    store["offset"] = 0
    store["seen_max"] = 0
    with _LOCK:
        _save_store(store)
    RECONCILE_STATE["count"] += 1
    RECONCILE_STATE["last_at"] = time.time()
    print(f"TG POLL queue regressed: offset {offset} -> 0 "
          f"(server latest {latest}); store reset", flush=True)
    return True


def reconcile_queue():
    """Flock-wrapped reconciliation for the poller thread start
    (start_poller). While another process holds the poll lock
    this is a no-op — that process's poller reconciles on its
    own schedule. Never raises."""
    try:
        lf = open(_poll_lock_path(), "a+")
    except Exception:
        lf = None  # lock file unusable: reconcile anyway
    if lf is not None:
        try:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lf.close()
            return False  # another poller is mid-cycle
    try:
        return _reconcile_queue_inner()
    except Exception:
        return False
    finally:
        if lf is not None:
            try:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            lf.close()


_CHAT_LOCKS = {}
_CH_LOCKS_G = threading.Lock()


def _chat_lock(cid):
    with _CH_LOCKS_G:
        lk = _CHAT_LOCKS.get(cid)
        if lk is None:
            lk = _CHAT_LOCKS[cid] = threading.Lock()
        return lk


def _dispatch(handler, payload):
    """Run one update's handler on its own thread (owner complaint
    2026-11-09: button taps felt slow). Handlers used to run inline
    in the polling loop, so one slow update — a photo download, a
    long API call — delayed every tap behind it, and the tap's
    answerCallbackQuery waited too. Now the loop only dispatches;
    a per-chat lock keeps ONE chat's updates in order (flow steps
    must not race each other) while other chats proceed at once."""
    # Message payloads carry chat at the top level; callback
    # payloads nest it under "message".
    chat = ((payload.get("message") or {}).get("chat")
            or payload.get("chat") or {})
    cid = str(chat.get("id")
              or (payload.get("from") or {}).get("id") or "?")

    def _run():
        with _chat_lock(cid):
            try:
                handler(payload)
            except Exception:
                # Build 245: never swallow silently again. A
                # handler that always throws is indistinguishable
                # from a dead bot from outside (2026-11-21: hours
                # of unanswered taps, nothing in the journal).
                # Log the traceback; dispatch still never raises
                # into the poll loop.
                print(f"TG handler error chat={cid}: "
                      f"{traceback.format_exc()}", flush=True)

    threading.Thread(target=_run, daemon=True).start()


# Build 199: outgoing duplicate guard. If any redelivery path
# still reaches a handler, the handler must not re-send the
# identical wizard text to the same chat seconds later — that
# repeat WAS the user-visible spam. A suppressed send returns the
# CACHED result of the original send, because two callers do use
# the return value: app.py's blur-piece picker stores
# res["message_id"] so later taps can edit that picker message,
# and the cached id is exactly the message really on screen.
# Keyed on (chat_id, text) only — buttons/keyboard do not make a
# repeat a different message. Delivery sends (send_video /
# send_document / send_photo) are NOT guarded.
_SENT_GUARD = {}
_SENT_GUARD_LOCK = threading.Lock()
_SENT_GUARD_WINDOW = 20.0


def send_message(chat_id, text, buttons=None, keyboard=None):
    tok = token()
    if not tok:
        raise RuntimeError("telegram bot token မရှိပါ")
    key = (str(chat_id), text)
    with _SENT_GUARD_LOCK:
        hit = _SENT_GUARD.get(key)
        if hit and time.time() - hit[0] < _SENT_GUARD_WINDOW:
            return hit[1]
    params = {"chat_id": str(chat_id), "text": text}
    if buttons:
        params["reply_markup"] = _markup(buttons)
    elif keyboard:
        params["reply_markup"] = json.dumps({
            "keyboard": [[{"text": t} for t in row] for row in keyboard],
            "resize_keyboard": True})
    result = _call(tok, "sendMessage", **params)
    with _SENT_GUARD_LOCK:
        _SENT_GUARD[key] = (time.time(), result)
        if len(_SENT_GUARD) > 512:  # opportunistic prune
            cutoff = time.time() - _SENT_GUARD_WINDOW
            for k in [k for k, v in _SENT_GUARD.items()
                      if v[0] < cutoff]:
                _SENT_GUARD.pop(k, None)
    return result


def send_alert(text):
    """Build 246 (watchdog): one plain-text alert to the FIXED
    admin chat. Deliberately bypasses send_message's duplicate
    guard (an alert must never be swallowed as a 'duplicate'),
    and when the configured API base fails — e.g. the LOCAL
    Bot API server is itself the thing that died — retries the
    same text ONCE via the cloud base. Returns True on delivery
    by either route; never raises."""
    tok = token()
    if not tok:
        return False
    try:
        _call(tok, "sendMessage", chat_id=str(FIXED_ADMIN_CHAT_ID),
              text=text)
        return True
    except Exception:
        pass
    try:
        import requests
        r = requests.post(f"{CLOUD_BASE}/bot{tok}/sendMessage",
                          data={"chat_id": str(FIXED_ADMIN_CHAT_ID),
                                "text": text}, timeout=30)
        return bool(r.json().get("ok"))
    except Exception:
        return False


def edit_message(chat_id, message_id, text, buttons=None):
    """Edit a sent message in place (the number-picker refreshes its
    selection line without spamming a new message per tap)."""
    tok = token()
    if not tok:
        raise RuntimeError("telegram bot token မရှိပါ")
    params = {"chat_id": str(chat_id), "message_id": int(message_id),
              "text": text}
    if buttons:
        params["reply_markup"] = _markup(buttons)
    return _call(tok, "editMessageText", **params)


def _markup(buttons):
    """buttons: rows of (label, callback_data) -> inline keyboard JSON."""
    return json.dumps({"inline_keyboard": [
        [{"text": label, "callback_data": data} for label, data in row]
        for row in buttons]})


def send_keyboard(chat_id, text, rows):
    """Persistent bottom menu (reply keyboard) — the navigation
    layer customers tap instead of typing commands."""
    tok = token()
    if not tok:
        raise RuntimeError("telegram bot token မရှိပါ")
    markup = json.dumps({
        "keyboard": [[{"text": t} for t in row] for row in rows],
        "resize_keyboard": True})
    return _call(tok, "sendMessage", chat_id=str(chat_id), text=text,
                 reply_markup=markup)


def send_photo(chat_id, photo_path, caption="", buttons=None):
    tok = token()
    if not tok:
        raise RuntimeError("telegram bot token မရှိပါ")
    import requests
    data = {"chat_id": str(chat_id), "caption": caption[:1024]}
    if buttons:
        data["reply_markup"] = _markup(buttons)
    with open(photo_path, "rb") as fh:
        r = requests.post(_api_url(tok, "sendPhoto"),
                          data=data,
                          files={"photo": (Path(photo_path).name, fh,
                                           "image/jpeg")},
                          timeout=120)
    payload = r.json()
    if not payload.get("ok"):
        raise RuntimeError(
            f"telegram sendPhoto: {payload.get('description')}")
    return payload["result"]


def send_document(chat_id, path, caption="", buttons=None):
    """Upload a small file as a document (build 244: the
    review flow's TXT exports). Mirrors send_photo's shape;
    like every delivery send it is NOT covered by the
    duplicate guard above — an explicit re-pull must always
    really send."""
    tok = token()
    if not tok:
        raise RuntimeError("telegram bot token မရှိပါ")
    import requests
    path = Path(path)
    data = {"chat_id": str(chat_id), "caption": caption[:1024]}
    if buttons:
        data["reply_markup"] = _markup(buttons)
    with open(path, "rb") as fh:
        r = requests.post(_api_url(tok, "sendDocument"),
                          data=data,
                          files={"document": (path.name, fh,
                                              "text/plain")},
                          timeout=120)
    payload = r.json()
    if not payload.get("ok"):
        raise RuntimeError(
            f"telegram sendDocument: {payload.get('description')}")
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
        # Build 245: reconcile once when the poller thread starts
        # — a queue regression that happened while the app was
        # down (or that took the previous process down) is healed
        # before the first poll, not after 20 empty ones.
        try:
            if token():
                reconcile_queue()
        except Exception:
            pass
        while True:
            try:
                if token():
                    poll_once()
                # Long polling already waited server-side; re-poll
                # almost immediately so queued updates drain fast.
                time.sleep(0.2)
            except Exception as e:
                LAST_POLL["error"] = f"{type(e).__name__}: {e}"[:200]
                time.sleep(3)

    _POLLER_THREAD = threading.Thread(target=loop, daemon=True)
    _POLLER_THREAD.start()


# Build 244: with a LOCAL Bot API server, getFile can take
# far longer than the shared _call default (30 s) to answer
# for a very large file — the server is still pulling it
# down from Telegram (the owner's 2-hour video died exactly
# there: "Read timed out. (read timeout=30)"). The local
# getFile therefore gets send_video's 600 s timeout and up
# to 3 attempts on a timeout / connection failure — the
# server keeps fetching in the background, so a later
# attempt can succeed once the file lands. Cloud mode is
# unchanged: one attempt at the default timeout (cloud
# getFile refuses >20 MB anyway, and callers check
# download_cap() first).
_GETFILE_TIMEOUT_S = 600
_GETFILE_ATTEMPTS = 3
_GETFILE_RETRY_PAUSE_S = 5


def _get_file(tok, file_id):
    import requests
    if not is_local():
        return _call(tok, "getFile", file_id=str(file_id))
    last_err = None
    for attempt in range(_GETFILE_ATTEMPTS):
        try:
            return _call(tok, "getFile",
                         timeout=_GETFILE_TIMEOUT_S,
                         file_id=str(file_id))
        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError) as e:
            last_err = e
            if attempt + 1 < _GETFILE_ATTEMPTS:
                time.sleep(_GETFILE_RETRY_PAUSE_S)
    raise last_err


def download_file(file_id, dest_path):
    """Bot file download (getFile). The cloud endpoint refuses bot
    downloads over 20 MB — the caller checks tg.download_cap()
    before calling (a local server lifts that cap entirely).

    With a LOCAL Bot API server in --local mode, getFile answers
    with an ABSOLUTE filesystem path (the server runs on this same
    VPS), so the file is copied straight off disk. A relative path
    (cloud, or a local server not in --local mode) is fetched over
    HTTP from the configured base instead."""
    tok = token()
    if not tok:
        raise RuntimeError("telegram bot token မရှိပါ")
    info = _get_file(tok, file_id)
    path = info.get("file_path")
    if not path:
        raise RuntimeError("telegram file မရပါ")
    # Via .part + rename: a dropped connection must not leave a
    # truncated file that later steps treat as the whole video.
    dest_path = Path(dest_path)
    tmp = dest_path.with_name(dest_path.name + ".part")
    if os.path.isabs(path):
        src = Path(path)
        if not src.exists():
            raise RuntimeError("telegram file မရပါ (local path missing)")
        shutil.copyfile(src, tmp)
        tmp.replace(dest_path)
        return dest_path
    import requests
    url = f"{api_base()}/file/bot{tok}/{path}"
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(chunk_size=1 << 18):
                fh.write(chunk)
    tmp.replace(dest_path)
    return dest_path


# ---------------- sending ----------------

MAX_BYTES = 50 * 1000 * 1000  # cloud api.telegram.org bot upload cap

# A local Bot API server (build 184) raises the protocol send cap
# to 2000 MB; the app keeps a small safety margin under it.
LOCAL_MAX_BYTES = 1900 * 1000 * 1000

# Cloud getFile refuses bot downloads over 20 MB (no such cap on a
# local server — see download_cap()).
MAX_DOWNLOAD_BYTES = 20 * 1000 * 1000


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
                    _api_url(tok, method),
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
