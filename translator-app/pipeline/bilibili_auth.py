"""Bilibili login session for downloads — build 236.

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
    web/qrcode/generate), shows it as an image, and polls
    qr_poll(); the owner scans it with the Bilibili app
    and confirms on his phone. On success the poll
    response's Set-Cookie headers carry the session
    (SESSDATA / bili_jct / DedeUserID / ...) — captured
    from the response cookie jar and stored. On a single
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
nothing about it may touch disk.
"""
import io
import os
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
_PENDING = {"key": None, "url": None, "ts": 0.0}
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
             "# Bilibili login session (build 236) — a "
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
    with _PENDING_LOCK:
        _PENDING.update({"key": None, "url": None, "ts": 0.0})


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


def qr_start():
    """Ask passport for a login QR and keep its key as
    the process-local pending login. Returns {"ok",
    "state", "scan_url"} — the scan URL is transient
    (it dies with the key) and is returned so the card
    can still offer it as text when QR rendering is
    unavailable; it is NOT the credential (the stored
    cookies are, and those are never returned)."""
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
        _PENDING.update({"key": key, "url": url,
                         "ts": time.time()})
    return {"ok": True, "state": "waiting", "scan_url": url}


def _pending_fresh():
    with _PENDING_LOCK:
        key, ts = _PENDING["key"], _PENDING["ts"]
    if not key or time.time() - ts > _PENDING_TTL_S:
        return None
    return key


def _drop_pending():
    with _PENDING_LOCK:
        _PENDING.update({"key": None, "url": None, "ts": 0.0})


def _capture_session(resp):
    """The poll success response's cookies -> stored
    records. requests exposes them as a cookie jar of
    http.cookiejar.Cookie objects (built from the
    Set-Cookie headers); every one of them belongs to
    the bilibili session. Returns the records, or []
    when the jar carries no SESSDATA (a "success" we
    cannot use — nothing is stored then)."""
    records = []
    try:
        jar = list(resp.cookies)
    except Exception:
        jar = []
    for c in jar:
        records.append({
            "domain": getattr(c, "domain", "") or ".bilibili.com",
            "path": getattr(c, "path", "") or "/",
            "secure": bool(getattr(c, "secure", False)),
            "expires": int(getattr(c, "expires", 0) or 0),
            "name": getattr(c, "name", ""),
            "value": getattr(c, "value", "")})
    records = [r for r in records if r["name"]]
    if not _sessdata(records):
        return []
    return records


def qr_poll():
    """One poll of the pending login. States: waiting
    (86101, not scanned yet), scanned (86090, scanned,
    awaiting the phone confirm), expired (86038, or the
    local pending TTL ran out), done (code 0 — session
    captured + stored), error (anything else; the
    pending login is KEPT so the card can keep polling
    through a transient failure)."""
    key = _pending_fresh()
    if not key:
        return {"state": "expired"}
    try:
        resp = _passport_get("/qrcode/poll",
                             params={"qrcode_key": key})
        payload = resp.json() or {}
    except Exception:
        return {"state": "error"}
    data = payload.get("data") or {}
    code = data.get("code")
    if code == 0:
        records = _capture_session(resp)
        if not records:
            return {"state": "error"}
        _write(records)
        _drop_pending()
        return {"state": "done", **status()}
    if code == 86101:
        return {"state": "waiting"}
    if code == 86090:
        return {"state": "scanned"}
    if code == 86038:
        _drop_pending()
        return {"state": "expired"}
    return {"state": "error"}


def qr_image_png():
    """The pending scan URL as a PNG QR code. Raises
    ValueError when no login is pending, or when the
    qrcode package is unavailable (the card then falls
    back to the scan URL as text)."""
    with _PENDING_LOCK:
        url, ts = _PENDING["url"], _PENDING["ts"]
    if not url or time.time() - ts > _PENDING_TTL_S:
        raise ValueError("QR မရှိသေးပါ — အရင် QR ထုတ်ပါ")
    try:
        import qrcode
    except Exception:
        raise ValueError("QR ထုတ်တဲ့ package မရှိသေးပါ")
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
