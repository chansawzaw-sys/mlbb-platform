"""Google ID-token verification for web sign-in (build 180).

The browser gets an ID token from Google Identity Services and
POSTs it to /api/auth/google; the server NEVER trusts it blind.
Verification goes through Google's own tokeninfo endpoint (stdlib
urllib — no new dependency): Google checks the signature and the
token's integrity, and this module then enforces the claims that
are ours to check — audience must equal OUR configured client id,
issuer must be Google, and the token must be unexpired. Any doubt
returns None and the caller refuses the sign-in.
"""
import json
import time
import urllib.parse
import urllib.request

_TOKENINFO = "https://oauth2.googleapis.com/tokeninfo?id_token="
_ISSUERS = ("accounts.google.com", "https://accounts.google.com")


def verify_id_token(id_token, client_id):
    """-> {"sub", "email", "email_verified"} or None.

    email_verified is normalised to a bool (tokeninfo reports it
    as the string "true"). Callers decide what an unverified email
    means — app.py rejects such sign-ins outright."""
    if not id_token or not client_id:
        return None
    try:
        url = _TOKENINFO + urllib.parse.quote(str(id_token), safe="")
        req = urllib.request.Request(url, headers={
            "User-Agent": "video-translator/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    if data.get("aud") != client_id:
        return None
    if data.get("iss") not in _ISSUERS:
        return None
    try:
        if int(data.get("exp") or 0) <= int(time.time()):
            return None
    except (TypeError, ValueError):
        return None
    sub = data.get("sub")
    email = (data.get("email") or "").strip().lower()
    if not sub or not email:
        return None
    verified = data.get("email_verified")
    return {"sub": str(sub), "email": email,
            "email_verified": verified is True or verified == "true"}
