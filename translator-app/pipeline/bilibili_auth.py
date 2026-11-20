"""Bilibili login session for downloads — build 238.

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

The passport calls route per purpose (build 238): the
generate call tries direct first, then the tunnel
proxy (pipeline.tunnel.requests_proxies) when one is
verified up; the POLL tries the tunnel FIRST — passport
is a mainland host, the tunnel is the natural route
from this server, and live on build 237 the poll path
failed from the VPS (the card sat in the "error" state
from the start and no session was ever captured) while
the same contract answered fine from a clean network.
A leg that answers with ANY HTTP status ends a poll
attempt — fall-through happens only on transport
exceptions.

Build 238 also gives the card eyes: the pending login
carries admin-visible diagnostics (last_error — an HTTP
status or an exception class + truncated message, the
pending key scrubbed out, NEVER a cookie value — and
last_via, the leg that answered), qr_start performs
ONE synchronous poll before returning so a broken poll
path shows its error on the card instantly instead of
after a scan, and an HTTP 412 poll answer is recorded
as the risk-control diagnostic it is while the login's
state stays waiting/scanned — the poller keeps trying
until the TTL instead of degrading to a bare "error".

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
            "state": None, "gen": 0,
            "last_error": "", "last_via": ""}
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
             "# Bilibili login session (build 238) — a "
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


def _diag_text(exc, key=None):
    """An exception -> the admin-visible diagnostic
    (build 238): the exception class + message, the
    pending qrcode_key scrubbed out (a requests error
    can quote the request URL, key and all), flattened
    and hard-truncated to ~120 chars. Passport transport
    errors never carry a cookie value, and the key is
    the only secret-shaped string they could quote —
    so what lands here is safe for the admin card."""
    if exc is None:
        return ""
    s = f"{type(exc).__name__}: {exc}"
    s = s.replace("\n", " ").replace("\r", " ").strip()
    if key:
        s = s.replace(key, "***")
    return s[:120]


def _passport_poll(key):
    """ONE passport poll GET, tunnel-first (build 238):
    passport is a mainland host and the tunnel is the
    natural route from this server, so when a tunnel
    proxy is verified up it is tried FIRST and the
    direct leg is the fallback. A leg that answers with
    ANY HTTP status ends the attempt — fall-through
    happens only on transport exceptions. Returns
    (resp, via, exc): resp None + exc set when every
    leg failed in transport; via names the leg that
    answered ("tunnel" / "direct", "" when none did)."""
    url = _PASSPORT + "/qrcode/poll"
    legs = []
    proxies = tunnel.requests_proxies()
    if proxies:
        legs.append(("tunnel", proxies))
    legs.append(("direct", None))
    last_exc = None
    for via, prox in legs:
        try:
            resp = requests.get(
                url, params={"qrcode_key": key},
                headers=_HEADERS, timeout=15, proxies=prox)
        except Exception as e:  # noqa: BLE001
            last_exc = e
            continue
        return resp, via, None
    return None, "", last_exc


def _reset_pending():
    """Drop the pending login and invalidate any poller
    thread still working on it (generation bump — a
    stale thread checks the generation before every poll
    and again before it may store, so it can never write
    a session for a superseded key)."""
    with _PENDING_LOCK:
        _PENDING.update({"key": None, "url": None,
                         "ts": 0.0, "state": None,
                         "gen": _PENDING["gen"] + 1,
                         "last_error": "", "last_via": ""})


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
                         "state": "waiting", "gen": gen,
                         "last_error": "", "last_via": ""})
        ts = _PENDING["ts"]
    # Build 238: ONE synchronous poll on this request
    # thread, BEFORE the daemon poller is spawned — it
    # is the login's first poll (the thread continues
    # from the second, one interval later), and its
    # outcome + diagnostics are already recorded when
    # this returns, so a broken poll path shows its
    # error on the card instantly instead of after a
    # scan. A transient failure here is SOFT: the
    # diagnostics are recorded but the state stays
    # "waiting" — the poller keeps trying regardless.
    try:
        finished = _poll_step(gen, key, soft_error=True)
    except Exception:
        finished = False
    if not finished:
        try:
            threading.Thread(target=_poll_loop,
                             args=(gen, key, ts), daemon=True,
                             name="bili-qr-poll").start()
        except Exception:
            pass  # the pending state still expires cleanly
    with _PENDING_LOCK:
        state = _PENDING["state"] \
            if _PENDING["gen"] == gen else None
    return {"ok": True, "state": state or "waiting",
            "scan_url": url}


def _finish_pending(gen, key, state, err=None, via=None):
    """Record a terminal state (expired / capture_failed)
    for the pending login IF it is still the current
    one; the key is dropped so nothing polls it again.
    err/via, when given, update the diagnostics in the
    same write (None leaves them as they were)."""
    with _PENDING_LOCK:
        if (_PENDING["gen"] == gen
                and _PENDING["key"] == key):
            _PENDING.update({"key": None, "state": state})
            if err is not None:
                _PENDING["last_error"] = err
            if via is not None:
                _PENDING["last_via"] = via


def _record_state(gen, key, state, err, via):
    """Record a non-terminal poll outcome IF the login
    is still the current one: state (None = leave the
    recorded state untouched — the HTTP 412 shape) plus
    the diagnostics, in one locked write."""
    with _PENDING_LOCK:
        if (_PENDING["gen"] == gen
                and _PENDING["key"] == key):
            if state is not None:
                _PENDING["state"] = state
            _PENDING["last_error"] = err
            _PENDING["last_via"] = via


def _poll_step(gen, key, soft_error=False):
    """ONE poll of the pending login + recording of its
    outcome (state and diagnostics). Returns True when
    the login is finished — done (stored, or discarded
    as superseded), expired, capture_failed — and False
    while it is still in flight. Single-writer
    discipline (build 237): the caller — qr_start's
    synchronous first poll, then the daemon thread — is
    the only passport poller and the only writer of the
    stored session on the QR path; every write is
    generation-checked under the lock, so a superseded
    login can never store. soft_error (the synchronous
    first poll only): a transient "error" outcome
    records ONLY the diagnostics and leaves the state
    at "waiting" — the card shows the failure detail
    under a still-pending login, and the poller keeps
    trying on its own cadence."""
    try:
        state, records, err, via = _poll_once(key)
    except Exception as e:  # _poll_once is total by
        # construction; this is belt + braces so a step
        # can never kill the poller thread silently.
        state, records, err, via = \
            "error", None, _diag_text(e, key), ""
    if state == "done":
        with _PENDING_LOCK:
            if (_PENDING["gen"] != gen
                    or _PENDING["key"] != key):
                return True  # superseded mid-poll: discard
            _write(records)
            _PENDING.update({"key": None, "state": "done",
                             "last_error": "",
                             "last_via": via})
        return True
    if state in ("expired", "capture_failed"):
        _finish_pending(gen, key, state, err, via)
        return True
    if state == "error" and soft_error:
        _record_state(gen, key, None, err, via)
        return False
    _record_state(gen, key, state, err, via)
    return False


def _poll_loop(gen, key, ts):
    """The pending login's owner poller: one daemon
    thread per qr_start, polling passport every
    _POLL_INTERVAL_S until the login resolves, the local
    TTL runs out, or the generation moves on (a newer
    qr_start, or clear()). The thread's first poll is
    one interval out — qr_start's synchronous poll was
    the login's first (build 238), so the cadence is
    unchanged, just owned by two callers of the one
    _poll_step. Defensive by construction — any
    unexpected failure degrades to recorded diagnostics
    and the loop carries on until the TTL, so the
    thread never dies silently with a login pending."""
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
        except Exception:
            return
        time.sleep(_POLL_INTERVAL_S)
        try:
            if _poll_step(gen, key):
                return
        except Exception as e:
            _record_state(gen, key, None,
                          _diag_text(e, key), "")


def _poll_once(key):
    """ONE passport poll of the pending key -> (state,
    records, last_error, last_via). States: waiting
    (86101, not scanned yet), scanned (86090, scanned,
    awaiting the phone confirm), expired (86038), done
    (code 0 with a captured session — records are ready
    to store), capture_failed (code 0 but NO SESSDATA
    could be assembled from any source — a loud dead
    end, never a silent wait), error (anything else;
    transient — the poller keeps the login and tries
    again), and None — NO state change (build 238: an
    HTTP 412 answer is Bilibili's risk control, recorded
    as the diagnostic "HTTP 412 (Bilibili risk control)"
    while the login's last real state stands). A clean
    200 answer clears last_error; last_via always names
    the leg that answered ("" when none did)."""
    resp, via, exc = _passport_poll(key)
    if resp is None:
        return "error", None, _diag_text(exc, key), ""
    if resp.status_code == 412:
        return None, None, \
            "HTTP 412 (Bilibili risk control)", via
    if resp.status_code != 200:
        return "error", None, f"HTTP {resp.status_code}", via
    try:
        payload = resp.json() or {}
    except Exception as e:
        return "error", None, _diag_text(e, key), via
    data = payload.get("data") or {}
    code = data.get("code")
    if code == 0:
        records = _assemble_session(resp, data)
        if not records:
            return "capture_failed", None, "", via
        return "done", records, "", via
    if code == 86101:
        return "waiting", None, "", via
    if code == 86090:
        return "scanned", None, "", via
    if code == 86038:
        return "expired", None, "", via
    return "error", None, "", via


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
    could be captured — retry with a fresh QR). Every
    shape also carries the build-238 diagnostics
    (last_error / last_via) — admin-only, and scrubbed
    by construction (see _diag_text): never a cookie
    value, never the qrcode_key."""
    with _PENDING_LOCK:
        key = _PENDING["key"]
        ts = _PENDING["ts"]
        state = _PENDING["state"]
        diag = {"last_error": _PENDING["last_error"],
                "last_via": _PENDING["last_via"]}
    if state == "done":
        return {"state": "done", **status(), **diag}
    if state == "capture_failed":
        return {"state": "capture_failed", **diag}
    if state == "expired":
        return {"state": "expired", **diag}
    if key and time.time() - ts <= _PENDING_TTL_S:
        return {"state": state or "waiting", **diag}
    return {"state": "expired", **diag}


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
