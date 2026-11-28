"""OpenAI translator — FIRST choice when a key is configured.

The owner already holds OpenAI API credit (bought earlier, unused —
seen 2026-11-10), so this engine needs no new purchase: a mini-class
model translates a whole video for roughly $0.002. Same contract as
pipeline/gemini_translate.py: ONE request for ALL segments, the same
mode-aware style prompts (imported, one source), strict validation;
ANY failure raises and the caller falls back (Gemini, then the free
chain).

Key handling (security): read ONLY from env OPENAI_API_KEY or the
git-ignored `.env` in the project root; saved via the app's settings
endpoint behind the admin login; never returned, never logged.

Build 276: the Admin card can head the TRANSLATION walk with
GPT-6 Luna (TRANSLATE_MODEL=luna — see the choice gate below).
The default stays the pre-276 walk byte for byte, and a Luna
404 falls through to gpt-4o-mini like any missing model, so
the choice can never fail a job by itself. Script generation
(generate_text) does NOT follow the choice.
"""
import json
import os
import re
import time
from pathlib import Path

from . import quota, tunnel
from .common import PROJECT_ROOT
from .gemini_translate import (_build_prompt, _extract_json_array,
                               _prompt_for, _validated_result)

_API_URL = "https://api.openai.com/v1/chat/completions"


def _models():
    """Configured model first, then the known mini-class names —
    a 404 (model not on this account) moves to the next one."""
    env = (os.environ.get("OPENAI_MODEL") or "").strip()
    out = []
    for m in [env, "gpt-4o-mini", "gpt-4.1-mini"]:
        if m and m not in out:
            out.append(m)
    return out


# --- Build 276: the translation-model choice ----------------
# TRANSLATE_MODEL, read ONCE at import from the process env,
# falling back to the project .env — the exact pattern of
# translate_segments' TRANSLATE_CHAIN gate, so a flip (Admin
# -> the OpenAI card, /api/settings/translate-model) takes
# effect on the next app start, never mid-job. "luna" heads
# the TRANSLATION walk with GPT-6 Luna; anything else (or
# unset) is "current", the pre-276 walk byte for byte.
# generate_text (Recap/Story/Commentary script generation,
# build 269) does NOT follow this choice — it keeps
# _models() exactly; this gate steers translation only.

LUNA_MODEL = "gpt-6-luna"


def _read_model_choice():
    v = (os.environ.get("TRANSLATE_MODEL") or "").strip().lower()
    if not v:
        try:
            env_file = PROJECT_ROOT / ".env"
            if env_file.exists():
                for line in env_file.read_text(
                        encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("TRANSLATE_MODEL="):
                        v = line.split("=", 1)[1].strip() \
                            .strip('"').strip("'").lower()
                        break
        except Exception:
            v = ""
    return "luna" if v == "luna" else "current"


_MODEL_CHOICE = _read_model_choice()


def model_choice():
    """The translation-model choice this process booted
    with: "current" or "luna" (surfaced by
    /api/settings/translate-model so the Admin card can
    show what is actually live versus what is saved)."""
    return _MODEL_CHOICE


def translate_models():
    """The model walk for TRANSLATION — translate_all,
    which the batch/chunk walk and the per-segment rescue
    share, so both follow the choice together. "current":
    _models() verbatim (pre-276 behaviour). "luna":
    gpt-6-luna first, then gpt-4o-mini, then gpt-4.1-mini —
    a 404 (model not on this account) falls through to
    gpt-4o-mini exactly like any missing model in the
    existing walk, so a wrong or unavailable Luna id can
    never fail a job by itself."""
    if _MODEL_CHOICE == "luna":
        out = []
        for m in [LUNA_MODEL, "gpt-4o-mini", "gpt-4.1-mini"]:
            if m not in out:
                out.append(m)
        return out
    return _models()


def _unsupported_param(text):
    """Build 276: the request parameter an OpenAI 400 blames
    as unsupported, or None. Narrow by design — only an
    explicit unsupported-parameter complaint counts (the
    error's code or message says so); the name comes from
    error.param, else from the quoted name in the standard
    message shape ("Unsupported parameter: 'temperature'
    is not supported with this model."). Any other 400 is
    an ordinary failure and earns no retry."""
    try:
        err = (json.loads(text or "{}") or {}).get("error") or {}
    except Exception:
        return None
    if not isinstance(err, dict):
        return None
    msg = err.get("message") or ""
    code = (err.get("code") or "").strip()
    if code != "unsupported_parameter" \
            and "unsupported parameter" not in msg.lower():
        return None
    param = (err.get("param") or "").strip()
    if param:
        return param
    m = re.search(r"'([A-Za-z0-9_.]+)'", msg)
    return m.group(1) if m else None


def get_api_key():
    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if key:
        return key
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("OPENAI_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def key_configured():
    return bool(get_api_key())


def _note_error(msg):
    try:
        (PROJECT_ROOT / ".openai_last_error").write_text(
            f"{int(time.time())} {msg}"[:400], encoding="utf-8")
    except Exception:
        pass


def translate_all(segments, style="subs", context=None):
    """segments: list of dicts with 'text'. Returns {index: burmese}.

    context (build 248): optional chunk carry-over block,
    threaded into the shared prompt builder; None keeps the
    pre-248 prompt byte for byte.

    Raises on ANY problem so the caller can fall back."""
    import requests

    key = get_api_key()
    if not key:
        raise RuntimeError("no OPENAI_API_KEY configured")
    # Build 189: direct OpenAI is territory-blocked from this
    # server, so when the owner's VPN tunnel is verified up the
    # calls leave through it (per-call `proxies`, nothing global;
    # None — requests' default — whenever the tunnel is down).
    # Build 196: kwargs from the one shared helper, like every
    # Gemini/OpenAI call site.
    prompt = _build_prompt(style, segments, context)
    body = {"messages": [{"role": "user", "content": prompt}]}
    attempts = []
    last_body = ""

    def _call(payload):
        return requests.post(
            _API_URL, **tunnel.request_kwargs(
                json=payload, timeout=120,
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"}))

    for mdl in translate_models():
        payload = {**body, "model": mdl}
        try:
            resp = _call(payload)
        except Exception as e:  # network-level: next model shape
            attempts.append(f"{mdl}=net:{e}"[:60])
            continue
        if resp.status_code == 400 and mdl == LUNA_MODEL:
            # Build 276: GPT-6 models are reasoning models and
            # may reject a request parameter outright. On a 400
            # that NAMES the parameter (and only then), retry
            # this model ONCE with that parameter removed —
            # never a blind retry, and never for the mini
            # models, whose path is unchanged.
            bad = _unsupported_param(resp.text)
            if bad:
                payload = {k: v for k, v in payload.items()
                           if k != bad}
                try:
                    resp = _call(payload)
                except Exception as e:
                    attempts.append(f"{mdl}=net:{e}"[:60])
                    continue
        if resp.status_code == 200:
            # Build 163: validate inside the walk — a bad-shape
            # reply costs this model only, the next one still runs.
            try:
                text_out = resp.json()["choices"][0][
                    "message"]["content"]
                return _validated_result(text_out, segments, "OpenAI")
            except Exception as e:
                attempts.append(f"{mdl}=badformat:{e}"[:70])
                continue
        # Build 275 (quota fast-fail): insufficient_quota is a
        # hard billing wall — the prepaid balance is gone and
        # no other model on this account will answer either.
        # Raise the wall at once instead of walking the rest.
        wall = quota.wall_message("openai", resp.status_code,
                                  resp.text)
        if wall:
            _note_error("OpenAI quota wall: " + wall)
            raise quota.QuotaWallError(wall)
        attempts.append(f"{mdl}={resp.status_code}")
        last_body = resp.text[:200]
        if resp.status_code in (401, 403):
            break  # bad key / no access — other models won't help
        # 404 (model missing) or anything else: try the next model.
    msg = ("OpenAI failed: " + "; ".join(attempts)
           + f" | last: {last_body}")
    _note_error(msg)
    raise RuntimeError(msg)


def generate_text(prompt):
    """Free-form text generation (build 211, for the Recap
    script): ONE chat request with the caller's prompt over the
    SAME model walk + tunnel kwargs as translate_all. Returns
    the model's raw text (prose — no JSON validation). Raises
    on ANY problem so the caller can fall back."""
    import requests

    key = get_api_key()
    if not key:
        raise RuntimeError("no OPENAI_API_KEY configured")
    body = {"messages": [{"role": "user", "content": prompt}]}
    attempts = []
    last_body = ""
    for mdl in _models():
        try:
            resp = requests.post(
                _API_URL, **tunnel.request_kwargs(
                    json={**body, "model": mdl}, timeout=120,
                    headers={"Authorization": f"Bearer {key}",
                             "Content-Type": "application/json"}))
        except Exception as e:  # network-level: next model shape
            attempts.append(f"{mdl}=net:{e}"[:60])
            continue
        if resp.status_code == 200:
            try:
                text_out = resp.json()["choices"][0][
                    "message"]["content"]
                if not (text_out or "").strip():
                    raise ValueError("empty generation")
            except Exception as e:
                attempts.append(f"{mdl}=badformat:{e}"[:70])
                continue
            return text_out
        wall = quota.wall_message("openai", resp.status_code,
                                  resp.text)
        if wall:
            _note_error("OpenAI quota wall: " + wall)
            raise quota.QuotaWallError(wall)
        attempts.append(f"{mdl}={resp.status_code}")
        last_body = resp.text[:200]
        if resp.status_code in (401, 403):
            break  # bad key / no access — other models won't help
    msg = ("OpenAI generation failed: " + "; ".join(attempts)
           + f" | last: {last_body}")
    _note_error(msg)
    raise RuntimeError(msg)


if __name__ == "__main__":
    print("openai key configured:", key_configured(),
          "| models:", _models(),
          "| translate walk:", translate_models())
