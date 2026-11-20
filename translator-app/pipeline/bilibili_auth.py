"""Bilibili login session for downloads — build 237.

Bilibili's risk control refuses this server outright: the
Hong Kong datacenter IP, the browser-impersonated ladder
(build 235) and even the Singapore tunnel exit are all
served the risk-control page state (__INITIAL_STATE__
error.trueCode == -403), which yt-dlp surfaces as a
spurious "login required". The one credential that passes
is a LOGGED-IN session — the extractor itself checks for
a SESSDATA cookie (yt_dlp/extractor/bilibili.py
_is_logged_in). So the owner logs HIS OWN Bilibili account
in once, here, and pipeline/download.py hands the stored
cookies to yt-dlp (the "cookiefile" opt) for bilibili
URLs only.

Owner flow (Admin card "🍪 Bilibili", /api/settings/
bilibili* — admin-only via the accounts guard, exactly
like the tunnel card):
  * QR login (primary): the card asks this module for a
    passport QR (passport.bilibili.com/x/passport-login/
    web/qrcode/generate) and shows it as an image; a
    server-side background thread then polls passport
    until the login resolves (build 237 — polling used
    to ride on the card's own status hits, which stalled
    whenever the owner switched to the Bilibili app to
    scan and the browser tab was suspended). The owner
    scans the QR with the Bilibili app and confirms on
    his phone. On success the session does NOT reliably
    arrive as Set-Cookie headers: Bilibili delivers it
    inside the poll payload's data.url — a crossDomain
    URL whose QUERY PARAMETERS carry the values
    (DedeUserID, DedeUserID__ckMd5, SESSDATA, bili_jct,
    sid, ...). Build 236 read only the cookie jar, the
    jar was empty, and a confirmed scan stored NOTHING
    (live failure, 2026-11-20). Capture now merges BOTH
    sources (jar first, URL parameters overriding), then
    fires one best-effort GET of that crossDomain URL
    with the assembled cookies and merges any Set-Cookie
    it returns; a non-empty SESSDATA is required before
    anything is stored, and a success with no SESSDATA
    anywhere surfaces as the loud state "capture_failed"
    instead of silently waiting forever. On a single
    phone he can screenshot the QR and pick it from the
    album inside the Bilibili app's scanner.
  * Paste fallback: a Netscape cookies.txt export for
    bilibili.com pasted into the card; validated to carry
    a SESSDATA entry for a bilibili domain before it is
    accepted, and only the bilibili lines are kept.

Storage + secrecy — the tunnel card's pattern, verbatim
in spirit: ONE canonical file, bilibili-cookies.txt in
the app dir (chmod 600, gitignored like tun-singbox.json;
the deploy's cp -r sync never deletes it), written in
Netscape cookie format from whichever flow succeeded.
The cookie values are a credential: NO endpoint ever
returns them — the status surface is {configured,
logged_in_as} with the account id masked. Deleting
clears the file and downloads go back to anonymous.

The passport calls try direct first, then the tunnel
proxy (pipeline.tunnel.requests_proxies) when one is
verified up — the passport host can itself be risk-
controlled from Hong Kong.

The pending QR (qrcode_key + scan URL) is process-local
state BY DESIGN, like app.py's registration throttle:
the app runs as a single Waitress process, the key dies
with the passport-side TTL anyway (~3 minutes), and
nothing about it may touch disk. The background poller
is a daemon thread guarded by a generation counter: a
second qr_start (or a delete) bumps the generation, and
a stale thread checks it before every poll and again
before it may store, so it can never write a session
for a superseded key. The card's poll endpoint is a
pure state read — it never calls passport itself.
"""
import io
import os
import urllib.parse
import threading
import time

import requests

from . import tunnel
from .common import PROJECT_ROOT

COOKIE_NAME = "bilibili-cookies.txt"

_PASSPORT = "https://passport.bilibili.com/x/passport-login/web"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
       "AppleWebKit/537.36 (KHTML, like Gecko) "
       "Chrome/126.0 Safari/537.36")
_HEADERS = {"User-Agent": _UA,
            "Referer": "https://www.bilibili.com/"}

_PENDING_TTL_S = 300  # the passport key itself lives ~180 s
_POLL_INTERVAL_S = 2.0  # background poller cadence
_PENDING = {"key": None, "url": None, "ts": 0.0,
            "state": None, "gen": 0}
_PENDING_LOCK = threading.Lock()

_MAX_PASTE_CHARS = 500_000


def _path():
    return PROJECT_ROOT / COOKIE_NAME


# ---------------- Netscape cookie file ----------------

def _parse_netscape(text):
    """Netscape cookies.txt text -> cookie records. The
    #HttpOnly_ prefix some exporters emit marks a real
    cookie line, not a comment. Malformed lines are
    skipped, never fatal — the SESSDATA check downstream
    is what decides acceptance."""
    out = []
    for raw in (text or "").splitlines():
        line = raw.rstrip("\n").rstrip("\r")
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, path, secure, expires, name = parts[:6]
        value = "\t".join(parts[6:])
        try:
            exp = int(expires)
        except (TypeError, ValueError):
            exp = 0
        out.append({"domain": domain, "path": path or "/",
                    "secure": secure.upper() == "TRUE",
                    "expires": exp, "name": name, "value": value})
    return out


def _to_netscape(records):
    lines = ["# Netscape HTTP Cookie File",
             "# Bilibili login session (build 237) — a "
             "credential; never share or commit this file."]
    for r in records:
        lines.append("\t".join([
            r["domain"] or ".bilibili.com",
            "TRUE" if (r["domain"] or "").startswith(".")
            else "FALSE",
            r["path"] or "/",
            "TRUE" if r.get("secure") else "FALSE",
            str(int(r.get("expires") or 0)),
            r["name"], r["value"]]))
    return "\n".join(lines) + "\n"


def _write(records):
    """Persist the canonical file: same-dir temp + atomic
    replace, chmod 600 (the tunnel config's discipline)."""
    p = _path()
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(_to_netscape(records), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass


def read_cookies():
    """The stored cookie records ([] when nothing is
    stored or the file is unreadable)."""
    try:
        return _parse_netscape(
            _path().read_text(encoding="utf-8"))
    except Exception:
        return []


def _sessdata(records):
    return [r for r in records
            if r["name"] == "SESSDATA"
            and "bilibili" in (r["domain"] or "")]


def configured():
    """A stored session that actually carries a bilibili
    SESSDATA — the only state download.py may act on."""
    return bool(_sessdata(read_cookies()))


def cookiefile_path():
    """The yt-dlp "cookiefile" value for bilibili
    downloads: the stored file's path while a session is
    configured, else None (anonymous — byte-for-byte the
    pre-236 behaviour). Read live on every download, so
    a login or a delete takes effect at once."""
    if configured():
        return str(_path())
    return None


def _account_id(records):
    for r in records:
        if r["name"] == "DedeUserID" and r["value"]:
            return r["value"]
    return ""


def _mask(uid):
    if not uid:
        return ""
    if len(uid) <= 5:
        return "***"
    return uid[:3] + "***" + uid[-2:]


def status():
    """Admin-card status: booleans + the MASKED account
    id ONLY. Never a cookie value, never the raw id."""
    records = read_cookies()
    return {"configured": bool(_sessdata(records)),
            "logged_in_as": _mask(_account_id(records))}


def clear():
    """Forget the session completely: file removed,
    downloads go back to anonymous."""
    try:
        _path().unlink(missing_ok=True)
    except Exception:
        pass
    _reset_pending()


# ---------------- paste fallback ----------------

def save_pasted(text):
    """Validate + store a pasted Netscape cookies.txt
    export. Only bilibili-domain lines are kept (a full
    browser export carries other sites' cookies — they
    are not this app's business and never reach disk).
    Raises ValueError (owner-facing Burmese) and stores
    NOTHING when no bilibili SESSDATA is present.
    Returns {"logged_in_as": masked id}."""
    text = (text or "").strip()
    if not text:
        raise ValueError("အရင် cookies.txt စာသား ထည့်ပါ")
    if len(text) > _MAX_PASTE_CHARS:
        raise ValueError("ထည့်တာ ကြီးလွန်းပါတယ် — bilibili.com "
                         "cookie တွေပဲ ထည့်ပါ")
    records = [r for r in _parse_netscape(text)
               if "bilibili" in (r["domain"] or "")]
    if not _sessdata(records):
        raise ValueError("SESSDATA cookie မတွေ့ပါ — bilibili.com "
                         "အတွက် login ဝင်ထားတဲ့ cookies.txt "
                         "ဖြစ်ရပါမယ်")
    _write(records)
    return {"logged_in_as": _mask(_account_id(records))}


# ---------------- passport QR login ----------------
# ---------------- passport QR login ----------------

def _passport_get(path, params=None):
    """One passport GET: direct first, then via the
    tunnel proxy when one is verified up (the passport
    host can itself be risk-controlled from Hong Kong).
    Returns the first HTTP-200 response; raises the last
    error when every route fails."""
    url = _PASSPORT + path
    attempts = [None]
    proxies = tunnel.requests_proxies()
    if proxies:
        attempts.append(proxies)
    last_exc = None
    last_resp = None
    for prox in attempts:
        try:
            resp = requests.get(url, params=params or {},
                                headers=_HEADERS, timeout=15,
                                proxies=prox)
        except Exception as e:  # noqa: BLE001
            last_exc = e
            continue
        if resp.status_code == 200:
            return resp
        last_resp = resp
    if last_resp is not None:
        return last_resp
    raise last_exc if last_exc is not None else RuntimeError(
        "passport request failed")


def _reset_pending():
    """Drop the pending login and invalidate any poller
    thread still working on it (generation bump — a
    stale thread checks the generation before every poll
    and again before it may store, so it can never write
    a session for a superseded key)."""
    with _PENDING_LOCK:
        _PENDING.update({"key": None, "url": None,
                         "ts": 0.0, "state": None,
                         "gen": _PENDING["gen"] + 1})


def qr_start():
    """Ask passport for a login QR, keep its key as the
    process-local pending login, and spawn the daemon
    thread that polls passport until it resolves (build
    237 — the card no longer has to stay awake for the
    login to land). Returns {"ok", "state", "scan_url"}
    — the scan URL is transient (it dies with the key)
    and is returned so the card can still offer it as
    text when QR rendering is unavailable; it is NOT the
    credential (the stored cookies are, and those are
    never returned). A second call replaces the pending
    login cleanly: the generation bump retires the old
    poller."""
    resp = _passport_get("/qrcode/generate")
    try:
        payload = resp.json() or {}
    except Exception:
        payload = {}
    data = payload.get("data") or {}
    key = data.get("qrcode_key") or ""
    url = data.get("url") or ""
    if payload.get("code") != 0 or not key or not url:
        raise ValueError("Bilibili က QR ထုတ်မပေးပါ — ခဏနေ "
                         "ပြန်စမ်းပါ")
    with _PENDING_LOCK:
        gen = _PENDING["gen"] + 1
        _PENDING.update({"key": key, "url": url,
                         "ts": time.time(),
                         "state": "waiting", "gen": gen})
        ts = _PENDING["ts"]
    try:
        threading.Thread(target=_poll_loop,
                         args=(gen, key, ts), daemon=True,
                         name="bili-qr-poll").start()
    except Exception:
        pass  # the pending state still expires cleanly
    return {"ok": True, "state": "waiting", "scan_url": url}


def _finish_pending(gen, key, state):
    """Record a terminal state (expired / capture_failed)
    for the pending login IF it is still the current
    one; the key is dropped so nothing polls it again."""
    with _PENDING_LOCK:
        if (_PENDING["gen"] == gen
                and _PENDING["key"] == key):
            _PENDING.update({"key": None, "state": state})


def _poll_loop(gen, key, ts):
    """The pending login's owner poller: one daemon
    thread per qr_start, polling passport every
    _POLL_INTERVAL_S until the login resolves, the local
    TTL runs out, or the generation moves on (a newer
    qr_start, or clear()). Single-writer discipline: this
    thread is the ONLY caller of the passport poll and
    the only writer of the stored session on the QR path;
    the card endpoint just reads the recorded state.
    Defensive by construction — any unexpected failure
    degrades to the transient "error" state and the loop
    carries on until the TTL, so the thread never dies
    silently with a login still pending."""
    while True:
        try:
            with _PENDING_LOCK:
                current = (_PENDING["gen"] == gen
                           and _PENDING["key"] == key)
            if not current:
                return
            if time.time() - ts > _PENDING_TTL_S:
                _finish_pending(gen, key, "expired")
                return
            state, records = _poll_once(key)
        except Exception:
            state, records = "error", None
        if state == "done":
            with _PENDING_LOCK:
                if (_PENDING["gen"] != gen
                        or _PENDING["key"] != key):
                    return  # superseded mid-poll: discard
                _write(records)
                _PENDING.update({"key": None,
                                 "state": "done"})
            return
        if state in ("expired", "capture_failed"):
            _finish_pending(gen, key, state)
            return
        with _PENDING_LOCK:
            if (_PENDING["gen"] == gen
                    and _PENDING["key"] == key):
                _PENDING["state"] = state
        time.sleep(_POLL_INTERVAL_S)


def _poll_once(key):
    """ONE passport poll of the pending key -> (state,
    records). States: waiting (86101, not scanned yet),
    scanned (86090, scanned, awaiting the phone
    confirm), expired (86038), done (code 0 with a
    captured session — records are ready to store),
    capture_failed (code 0 but NO SESSDATA could be
    assembled from any source — a loud dead end, never a
    silent wait), error (anything else; transient — the
    poller keeps the login and tries again)."""
    try:
        resp = _passport_get("/qrcode/poll",
                             params={"qrcode_key": key})
        payload = resp.json() or {}
    except Exception:
        return "error", None
    data = payload.get("data") or {}
    code = data.get("code")
    if code == 0:
        records = _assemble_session(resp, data)
        if not records:
            return "capture_failed", None
        return "done", records
    if code == 86101:
        return "waiting", None
    if code == 86090:
        return "scanned", None
    if code == 86038:
        return "expired", None
    return "error", None


def _jar_records(resp):
    """A response's cookie jar -> records. requests
    exposes the jar as http.cookiejar.Cookie objects
    built from the Set-Cookie headers; every one of them
    belongs to the bilibili session."""
    records = []
    try:
        jar = list(resp.cookies)
    except Exception:
        jar = []
    for c in jar:
        rec = {"domain": getattr(c, "domain", "")
               or ".bilibili.com",
               "path": getattr(c, "path", "") or "/",
               "secure": bool(getattr(c, "secure", False)),
               "expires": int(getattr(c, "expires", 0)
                              or 0),
               "name": getattr(c, "name", ""),
               "value": getattr(c, "value", "")}
        if rec["name"]:
            records.append(rec)
    return records


def _url_records(url):
    """The crossDomain session URL -> records. On QR
    success Bilibili delivers the session as QUERY
    PARAMETERS of data.url (DedeUserID,
    DedeUserID__ckMd5, SESSDATA, bili_jct, sid, ...) —
    the values are the credential, planted on the
    sibling domains when a browser follows the URL.
    Written here as .bilibili.com records (path /,
    session expiry — the jar the app hands yt-dlp lives
    for the process, exactly like a browser session)."""
    records = []
    if not url:
        return records
    try:
        query = urllib.parse.urlparse(url).query
        pairs = urllib.parse.parse_qsl(query)
    except Exception:
        return records
    for name, value in pairs:
        if not name or not value:
            continue
        records.append({"domain": ".bilibili.com",
                        "path": "/", "secure": True,
                        "expires": 0, "name": name,
                        "value": value})
    return records


def _crossdomain_records(url, records):
    """ONE best-effort GET of the crossDomain URL with
    the assembled cookies (direct first, then the tunnel
    proxies — the _passport_get route order), merging
    whatever Set-Cookie it answers with: following the
    URL is how the session gets planted, and the plant
    can carry cookies the parameters did not. Failures
    NEVER block: no URL, a non-bilibili host, a transport
    error or a non-200 all just return []."""
    if not url:
        return []
    try:
        parts = urllib.parse.urlparse(url)
    except Exception:
        return []
    host = parts.hostname or ""
    if parts.scheme not in ("http", "https"):
        return []
    if host != "bilibili.com" \
            and not host.endswith(".bilibili.com"):
        return []
    cookies = {r["name"]: r["value"] for r in records
               if r["name"]}
    attempts = [None]
    proxies = tunnel.requests_proxies()
    if proxies:
        attempts.append(proxies)
    for prox in attempts:
        try:
            resp = requests.get(url, headers=_HEADERS,
                                timeout=15, proxies=prox,
                                cookies=cookies or None)
        except Exception:
            continue
        if resp.status_code == 200:
            return _jar_records(resp)
    return []


def _assemble_session(resp, data):
    """The poll success -> the session records, from
    EVERY source Bilibili uses (build 237): the response
    cookie jar first, then the data.url query parameters
    overriding/adding, then the Set-Cookie of one
    best-effort crossDomain GET. Returns [] when no
    SESSDATA can be assembled from any of them — the
    caller turns that into "capture_failed" and stores
    NOTHING."""
    merged = {}
    for r in _jar_records(resp):
        merged[r["name"]] = r
    url = (data or {}).get("url") or ""
    for r in _url_records(url):
        merged[r["name"]] = r
    for r in _crossdomain_records(url,
                                  list(merged.values())):
        merged[r["name"]] = r
    records = list(merged.values())
    if not _sessdata(records):
        return []
    return records


def qr_poll():
    """The card's status read — PURE state, no passport
    call (build 237: the background poller owns polling,
    so a suspended browser tab can no longer stall a
    login; this only reports what the poller recorded).
    States: waiting / scanned (login in flight), error
    (transient — the poller is still trying), done
    (session captured + stored), expired (the key died,
    or nothing is pending — the build-236 shape),
    capture_failed (the scan was accepted but no session
    could be captured — retry with a fresh QR)."""
    with _PENDING_LOCK:
        key = _PENDING["key"]
        ts = _PENDING["ts"]
        state = _PENDING["state"]
    if state == "done":
        return {"state": "done", **status()}
    if state == "capture_failed":
        return {"state": "capture_failed"}
    if state == "expired":
        return {"state": "expired"}
    if key and time.time() - ts <= _PENDING_TTL_S:
        return {"state": state or "waiting"}
    return {"state": "expired"}


def qr_image_png():
    """The pending scan URL as a PNG QR code. Raises
    ValueError when no login is pending, or when the
    qrcode package is unavailable (the card then falls
    back to the scan URL as text)."""
    with _PENDING_LOCK:
        key = _PENDING["key"]
        url, ts = _PENDING["url"], _PENDING["ts"]
    if not key or not url \
            or time.time() - ts > _PENDING_TTL_S:
        raise ValueError("QR မရှိသေးပါ — အရင် QR ထုတ်ပါ")
    try:
        import qrcode
    except Exception:
        raise ValueError("QR ထုတ်တဲ့ package မရှိသေးပါ")
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
