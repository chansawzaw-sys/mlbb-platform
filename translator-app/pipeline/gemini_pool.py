"""Shared Gemini API-key pool (build 229).

The owner holds several Google accounts with several AI Studio
keys each (~25 keys), so ONE key is no longer the unit of
configuration: the Admin Gemini card saves a POOL of up to
MAX_KEYS keys (bulk paste, one per line) and every Gemini
consumer — translation (pipeline/gemini_translate.py) and the
Gemini TTS voices (pipeline/gemini_tts.py) — draws from this
one module, so rotation behaviour can never drift between them.

Storage: the keys live in the project .env as GEMINI_API_KEYS
(comma-joined, chmod 600 via pipeline.envfile, NEVER returned
by any API — the settings status reports COUNTS only). A
pre-229 install with only GEMINI_API_KEY set reads as a pool
of one. Reads follow the house pattern (os.environ first, then
the .env file, fresh on every call — admin saves take effect
without a restart).

Failure classes (the rotation contract):
  * quota / rate — HTTP 429, or a body carrying
    RESOURCE_EXHAUSTED / quota wording: the key is marked
    COOLING for COOLDOWN_S and the caller retries with the
    next key. Cooling is process-local (a restart simply
    re-tries every key, which is harmless).
  * quota_day (build 275) — a 429 whose body names a
    PER-DAY quota: the key cools for COOLDOWN_DAY_S (it is
    dead until tomorrow), and a caller whose every key
    lands here fails fast via pipeline.quota instead of
    re-walking the dead pool per segment.
  * invalid — the key ITSELF is dead: HTTP 400 with
    API_KEY_INVALID, or HTTP 403 with PERMISSION_DENIED /
    "API key not valid" wording (and no quota signature —
    quota is judged first). The key is DISABLED persistently:
    its SHA-256 hash (never the key) is recorded in
    .gemini_key_state.json next to the .env, and later jobs
    skip it without spending a call. Saving or deleting the
    pool from the Admin card clears the disabled marks (a
    fresh paste is a fresh start).
  * anything else (5xx, network, location blocks, bad reply
    shapes) is NEITHER: callers keep their legacy behaviour.

ordered_keys() is the one ordering every consumer uses:
usable keys first (in saved order), cooling keys last (as a
last resort — better a maybe-recovered key than a failed
job), disabled keys excluded entirely.
"""
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

from .common import PROJECT_ROOT
from .quota import _is_day_quota

MAX_KEYS = 30
COOLDOWN_S = 120.0
# Build 275: a key that hit its DAILY quota rests for hours,
# not COOLDOWN_S — it cannot serve again today, and a short
# cooldown would have every later segment re-walk it.
COOLDOWN_DAY_S = 6 * 3600.0

ENV_PATH = PROJECT_ROOT / ".env"
STATE_PATH = PROJECT_ROOT / ".gemini_key_state.json"

_LOCK = threading.Lock()
_cooling = {}          # key -> unix ts when the cooldown ends
_disabled_hashes = None  # None = not loaded from disk yet


def _env_raw(name):
    """One setting's raw value: os.environ first, then the
    project .env file (module-level ENV_PATH, so tests can
    redirect it), read fresh on every call."""
    v = (os.environ.get(name) or "").strip()
    if v:
        return v
    try:
        if ENV_PATH.exists():
            for line in ENV_PATH.read_text(
                    encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def _split_keys(raw):
    out, seen = [], set()
    for part in re.split(r"[\s,;]+", raw or ""):
        p = part.strip().strip('"').strip("'")
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def get_keys():
    """The saved pool, in saved order, deduped. Falls back to
    the legacy single GEMINI_API_KEY (a pre-229 install is a
    pool of one)."""
    keys = _split_keys(_env_raw("GEMINI_API_KEYS"))
    if not keys:
        keys = _split_keys(_env_raw("GEMINI_API_KEY"))
    return keys[:MAX_KEYS]


def key_configured():
    return bool(get_keys())


def key_count():
    return len(get_keys())


# ---------- failure classification ----------

def classify_error(status, body_text):
    """Judge one failed Gemini response: "quota_day",
    "quota", "invalid" or None. Quota is judged FIRST (a 403
    carrying quota wording is a quota failure, not a dead
    key); the invalid class is reserved for failures keyed
    to the key itself. Build 275: a 429 whose body names a
    PER-DAY quota is its own class — that key is dead until
    tomorrow, not for COOLDOWN_S, and when EVERY key is in
    that state the caller fails the job fast (pipeline.quota)
    instead of re-walking dead keys for every segment."""
    body = str(body_text or "")
    low = body.lower()
    if status == 429 and _is_day_quota(body):
        return "quota_day"
    if status == 429 or "resource_exhausted" in low \
            or "quota" in low or "rate limit" in low \
            or "rate_limit" in low:
        return "quota"
    if status == 400 and ("api_key_invalid" in low
                          or "api key not valid" in low):
        return "invalid"
    if status == 403 and ("permission_denied" in low
                          or "api_key_invalid" in low
                          or "api key not valid" in low):
        return "invalid"
    return None


# ---------- cooling (process-local) ----------

def mark_cooling(key, seconds=None):
    with _LOCK:
        _cooling[key] = time.time() + (COOLDOWN_S if seconds is None
                                       else float(seconds))


def mark_cooling_day(key):
    """Build 275: the long rest for a daily-quota-dead key."""
    mark_cooling(key, COOLDOWN_DAY_S)


def is_cooling(key):
    with _LOCK:
        until = _cooling.get(key)
    return bool(until and until > time.time())


# ---------- disabled (persistent, hash-only) ----------

def _hash(key):
    return hashlib.sha256(str(key).encode("utf-8")).hexdigest()


def _load_disabled():
    global _disabled_hashes
    if _disabled_hashes is None:
        hashes = set()
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            for h in data.get("disabled") or []:
                if isinstance(h, str):
                    hashes.add(h)
        except Exception:
            pass
        _disabled_hashes = hashes
    return _disabled_hashes


def _save_disabled():
    try:
        STATE_PATH.write_text(json.dumps(
            {"disabled": sorted(_disabled_hashes or set())}),
            encoding="utf-8")
    except Exception:
        pass


def mark_disabled(key):
    """Persistently disable a key whose failure is keyed to
    the key itself. Only the key's hash is stored — the value
    never touches disk outside the .env pool itself."""
    with _LOCK:
        hashes = _load_disabled()
        h = _hash(key)
        if h not in hashes:
            hashes.add(h)
            _save_disabled()


def is_disabled(key):
    with _LOCK:
        return _hash(key) in _load_disabled()


def disabled_count():
    """How many of the CURRENT pool's keys are disabled —
    the settings status number (a count, never a value)."""
    with _LOCK:
        hashes = _load_disabled()
    return sum(1 for k in get_keys() if _hash(k) in hashes)


def reset_disabled():
    """Clear every disabled mark (the Admin card calls this
    on pool save/delete: a fresh paste is a fresh start)."""
    global _disabled_hashes
    with _LOCK:
        _disabled_hashes = set()
        _save_disabled()
        _cooling.clear()


def ordered_keys():
    """The rotation order: usable keys in saved order, then
    cooling keys (last resort), disabled keys excluded."""
    keys = get_keys()
    with _LOCK:
        hashes = set(_load_disabled())
        now = time.time()
        cooling = {k for k, until in _cooling.items()
                   if until > now}
    usable = [k for k in keys
              if _hash(k) not in hashes and k not in cooling]
    cooled = [k for k in keys
              if _hash(k) not in hashes and k in cooling]
    return usable + cooled


# ---------- Admin-card parsing ----------

def _key_format_ok(key):
    """The Gemini key formats (the pre-229 card's rule):
    legacy "AIza…" keys and the "AQ.…" auth keys AI Studio
    has issued since May 2026."""
    return (key.startswith("AIza") or key.startswith("AQ.")) \
        and 20 <= len(key) <= 80 \
        and all(ch.isalnum() or ch in "._-" for ch in key)


def parse_keys(raw):
    """Validate an Admin-card paste -> (keys, None) or
    (None, Burmese error). Accepts a list, or one string split
    on newlines / commas / semicolons / spaces — a one-line
    paste is a pool of one, exactly the pre-229 behaviour.
    Deduped, order preserved, capped at MAX_KEYS."""
    if isinstance(raw, (list, tuple)):
        parts = [str(x) for x in raw]
    else:
        parts = re.split(r"[\s,;]+", str(raw or ""))
    keys, seen, bad = [], set(), 0
    for part in parts:
        p = part.strip().strip('"').strip("'")
        if not p:
            continue
        if not _key_format_ok(p):
            bad += 1
            continue
        if p not in seen:
            seen.add(p)
            keys.append(p)
    if bad:
        return None, (f"key {bad} ခု မမှန်ပါ — Gemini key "
                      "(AQ. သို့ AIza နဲ့ စတာ) ဖြစ်ရပါမယ်")
    if not keys:
        return None, "key အရင် ထည့်ပါ"
    if len(keys) > MAX_KEYS:
        return None, f"key အများဆုံး {MAX_KEYS} ခု ပဲ ရပါတယ်"
    return keys, None
