"""ElevenLabs premium dub voices (build 205) — design first.

The owner manages a registry of ElevenLabs voices (his clones or
library voices) in the Admin card; customers see them in the
dub-voice picker as a 🌟 Premium group billed at an admin-set
per-minute credit rate. The structure ships EMPTY: with no key
and no registry the picker payload, the bot voice step and the
quote math are byte-for-byte the pre-205 behaviour, and the
group lights up the moment the owner saves a key + a voice.

Settings live in the project .env (admin card, chmod 600, never
in git), read live exactly like the Gemini key (os.environ first,
then the .env file — pipeline/gemini_translate.py's pattern), so
an admin save takes effect without a restart:

  ELEVENLABS_API_KEY         the key; NEVER returned by any API
  ELEVENLABS_VOICES          JSON list [{"id": ..., "name": ...}]
  ELEVENLABS_MODEL           eleven_v4_turbo (default) | eleven_v4
  ELEVENLABS_CREDITS_PER_MIN premium rate, integer, default 2
  ELEVENLABS_ACCESS          "test" (default) | "all" (build 206)
  ELEVENLABS_TEST_USERS      comma-separated usernames allowed
                             in test mode (build 206); defaults
                             to htetheather,heatherhh when the
                             key is ABSENT — a present-but-empty
                             value is the owner's explicit "no
                             test users" and wins over the default

A job carries the pick as voice = "el:<voice_id>" — unambiguous
next to the edge-tts style keys on both surfaces (web + bot).
run_job routes those jobs here instead of pipeline.tts.

Failure rule (owner's premium promise): if the key is missing,
the voice was removed, or the API errors, synthesize() raises a
polite Burmese RuntimeError and the job FAILS — it is never
charged (charge-on-success) and NEVER silently re-voiced with
edge-tts. There is deliberately no fallback ladder in this
module, unlike the free path in pipeline/tts.py.

API shape (verified from ElevenLabs docs, 2026-11-15):
POST {API_BASE}/v1/text-to-speech/{voice_id}, header
xi-api-key, JSON {"text", "model_id", "language_code": "my",
"voice_settings": {"stability", "similarity_boost"}} — the v4
models accept ONLY those two voice settings. model_id is always
sent explicitly: the endpoint's own default is a legacy model
with no Burmese. Billing is per character, by model only — a
clone costs exactly what a library voice costs.
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from .common import (PROJECT_ROOT, ensure_system_ca, load_segments,
                     probe_duration, save_segments)

API_BASE = "https://api.elevenlabs.io"
ENV_PATH = PROJECT_ROOT / ".env"

MODELS = ("eleven_v4_turbo", "eleven_v4")
DEFAULT_MODEL = "eleven_v4_turbo"
DEFAULT_CREDITS_PER_MIN = 2
EL_PREFIX = "el:"
STABILITY = 0.5
SIMILARITY_BOOST = 0.75
_REQUEST_TIMEOUT = 60
_ATTEMPTS = 3

FAIL_MSG = ("အသံ ထုတ်တာ မရပါ — ElevenLabs service ခဏ အလုပ်မလုပ်ပါ။ "
            "ခဏနေမှ ပြန်စမ်းပါ (credit လုံးဝ မဖြတ်ပါ)")


def _env_raw(key):
    """One setting's raw value: os.environ first, then the
    project .env file, read fresh on every call (admin edits
    take effect without a restart)."""
    v = (os.environ.get(key) or "").strip()
    if v:
        return v
    try:
        if ENV_PATH.exists():
            for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def get_api_key():
    return _env_raw("ELEVENLABS_API_KEY").strip('"').strip("'")


def key_configured():
    return bool(get_api_key())


def get_voices():
    """The registry as [{"id": voice_id, "name": display}].
    Missing or malformed data reads as an EMPTY registry (the
    feature stays invisible) rather than breaking the picker."""
    raw = _env_raw("ELEVENLABS_VOICES")
    if not raw:
        return []
    if raw.startswith('"'):
        try:
            unq = json.loads(raw)
            if isinstance(unq, str):
                raw = unq
        except Exception:
            pass
    try:
        data = json.loads(raw)
    except Exception:
        return []
    out = []
    if isinstance(data, list):
        for e in data:
            if isinstance(e, dict) and e.get("id"):
                out.append({"id": str(e["id"]),
                            "name": str(e.get("name") or e["id"])})
    return out


def find_voice(voice_id):
    for v in get_voices():
        if v["id"] == voice_id:
            return v
    return None


def get_model():
    m = _env_raw("ELEVENLABS_MODEL")
    return m if m in MODELS else DEFAULT_MODEL


def credits_per_min():
    try:
        return max(1, min(100, int(
            _env_raw("ELEVENLABS_CREDITS_PER_MIN")
            or DEFAULT_CREDITS_PER_MIN)))
    except (TypeError, ValueError):
        return DEFAULT_CREDITS_PER_MIN


# ---------- Access gate (build 206) ----------
# While the owner tests the premium voices they are NOT on sale:
# ELEVENLABS_ACCESS = "test" (the default) restricts them to
# admin accounts + the usernames in ELEVENLABS_TEST_USERS. The
# owner flips ELEVENLABS_ACCESS to "all" from the Admin card
# when he starts selling — no deploy. EVERY surface (web picker,
# web job creation, bot step, bot taps, bot pre-check) judges
# through elevenlabs_allowed() so the surfaces can never drift.

ACCESS_MODES = ("test", "all")
DEFAULT_ACCESS = "test"
DEFAULT_TEST_USERS = ("htetheather", "heatherhh")


def _env_has(key):
    """True when the key EXISTS at all (os.environ or a .env
    line), even with an empty value — unlike _env_raw, which
    cannot tell "absent" from "present but empty". The gate
    needs the distinction: an absent ELEVENLABS_TEST_USERS
    means the shipped defaults, a present-but-empty one is the
    owner deliberately clearing the list."""
    if key in os.environ:
        return True
    try:
        if ENV_PATH.exists():
            for line in ENV_PATH.read_text(
                    encoding="utf-8").splitlines():
                if line.strip().startswith(key + "="):
                    return True
    except Exception:
        pass
    return False


def get_access_mode():
    m = _env_raw("ELEVENLABS_ACCESS").strip().lower()
    return m if m in ACCESS_MODES else DEFAULT_ACCESS


def get_test_users():
    """The test-mode username list, as stored (case preserved;
    comparisons lowercase both sides — usernames are
    case-insensitive in this app). Absent key -> the defaults;
    present key -> exactly what the owner saved, empty included."""
    if not _env_has("ELEVENLABS_TEST_USERS"):
        return list(DEFAULT_TEST_USERS)
    raw = _env_raw("ELEVENLABS_TEST_USERS")
    return [u.strip() for u in raw.split(",") if u.strip()]


def parse_test_users(value):
    """Validate an Admin-card test-users value (a comma string
    or a list) -> (usernames, None) or (None, Burmese error).
    Entries are deduped case-insensitively, first spelling kept."""
    if isinstance(value, (list, tuple)):
        parts = [str(v) for v in value]
    else:
        parts = str(value or "").split(",")
    out, seen = [], set()
    for part in parts:
        u = part.strip()
        if not u:
            continue
        if len(u) > 40 or any(ch.isspace() for ch in u) \
                or not all(ch.isalnum() or ch in "_.-" for ch in u):
            return None, ("စမ်းသပ် username မမှန်ပါ: " + u)
        if u.lower() not in seen:
            seen.add(u.lower())
            out.append(u)
    return out, None


def elevenlabs_allowed(user):
    """THE gate. Mode "all" -> everyone (the owner opened the
    premium voices for sale). Test mode -> admins and listed
    test usernames only; anonymous / no account -> False."""
    if get_access_mode() == "all":
        return True
    if not user:
        return False
    if user.get("role") == "admin":
        return True
    uname = (user.get("username") or "").strip().lower()
    if not uname:
        return False
    return uname in {u.lower() for u in get_test_users()}


def is_el_voice(value):
    return bool(value) and str(value).startswith(EL_PREFIX)


def voice_id_of(value):
    return str(value)[len(EL_PREFIX):]


def picker_entries(user=None):
    """Picker entries for /api/voices and the bot voice step.
    EMPTY unless a key is set AND the registry has voices — an
    unconfigured install shows customers nothing new — AND the
    given user passes the build-206 access gate (test mode:
    admins + test usernames only, so a customer never even sees
    the premium group while the owner is testing)."""
    if not key_configured():
        return []
    if not elevenlabs_allowed(user):
        return []
    rate = credits_per_min()
    return [{"id": EL_PREFIX + v["id"], "label": v["name"],
             "premium": True, "rate": rate} for v in get_voices()]


def _synth_one(key, model, voice_id, text, out_path):
    """One segment, up to _ATTEMPTS tries with a short backoff.
    Auth failures (401/403) fail fast — retrying a dead key per
    segment would only multiply the wait. Any other failure
    raises RuntimeError(FAIL_MSG) after the attempts: the caller
    fails the whole job loudly (no substitute voice, no partial
    dub shipped as if it were the premium pick)."""
    url = f"{API_BASE}/v1/text-to-speech/{voice_id}"
    body = {"text": text, "model_id": model, "language_code": "my",
            "voice_settings": {"stability": STABILITY,
                               "similarity_boost": SIMILARITY_BOOST}}
    last = None
    for attempt in range(_ATTEMPTS):
        try:
            r = requests.post(
                url, headers={"xi-api-key": key,
                              "Content-Type": "application/json"},
                json=body, timeout=_REQUEST_TIMEOUT)
            if r.status_code == 200 and r.content:
                Path(out_path).write_bytes(r.content)
                return
            last = f"HTTP {r.status_code}"
            if r.status_code in (401, 403):
                break
        except Exception as e:  # network blip — back off, retry
            last = str(e)[:120]
        if attempt + 1 < _ATTEMPTS:
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(FAIL_MSG + (f" [{last}]" if last else ""))


def synthesize(workdir, voice_id):
    """Per-segment ElevenLabs synthesis with the SAME output
    contract as pipeline.tts.synthesize: writes tts/seg_XXX.mp3
    and sets tts_file / tts_duration / tts_provider on every
    segment of segments_my.json — plus tts_engine / tts_voice /
    tts_voice_name, so the job record shows what actually spoke.
    The retime step consumes the files unchanged."""
    key = get_api_key()
    if not key:
        raise RuntimeError(
            "ElevenLabs key မထည့်ရသေးပါ — admin က key ထည့်ပြီးမှ "
            "ဒီ အသံ သုံးလို့ရပါမယ် (credit လုံးဝ မဖြတ်ပါ)")
    entry = find_voice(voice_id)
    if entry is None:
        raise RuntimeError(
            "ရွေးထားတဲ့ ElevenLabs အသံ မရှိတော့ပါ — အသံ ပြန်ရွေးပေးပါ "
            "(credit လုံးဝ မဖြတ်ပါ)")
    ensure_system_ca()
    workdir = Path(workdir)
    segs = load_segments(workdir / "segments_my.json")
    tts_dir = workdir / "tts"
    tts_dir.mkdir(exist_ok=True)
    jobs = [(i, (seg.get("text_my") or "").strip(),
             tts_dir / f"seg_{i:03d}.mp3")
            for i, seg in enumerate(segs)]
    jobs = [(i, t, o) for i, t, o in jobs if t]
    model = get_model()
    if jobs:
        def _one(job_args):
            _i, _t, _o = job_args
            _synth_one(key, model, voice_id, _t, _o)
        with ThreadPoolExecutor(max_workers=4) as ex:
            # list() surfaces the first segment failure here; the
            # pool's shutdown still waits out the in-flight calls
            # (each bounded by the request timeout).
            list(ex.map(_one, jobs))
    for i, text, out in jobs:
        seg = segs[i]
        seg["tts_file"] = str(out)
        seg["tts_duration"] = round(probe_duration(out), 3)
        seg["tts_provider"] = f"elevenlabs:{model}"
        seg["tts_engine"] = "elevenlabs"
        seg["tts_voice"] = voice_id
        seg["tts_voice_name"] = entry["name"]
    save_segments(segs, workdir / "segments_my.json")
    return segs


if __name__ == "__main__":
    res = synthesize(Path(sys.argv[1]), sys.argv[2])
    print(f"{len(res)} segments synthesized via ElevenLabs "
          f"({get_model()})")
