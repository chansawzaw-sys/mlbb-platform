"""xKiro translator — build 155 (owner chose this route 2026-11-11
after seeing its pricing page; Muse verified the service and its
live catalog the same night).

xKiro is a young OpenAI-compatible API gateway (base
https://api.xkiro.com/v1). What makes it interesting here: a free
tier (500K tokens/day, 1M after Telegram verification which also
grants $5 of paid-model credit for 30 days), and DeepSeek V4 Flash
at $0.075/$0.30 per 1M tokens — cheaper than OpenRouter's DeepSeek.
The LIVE catalog (fetched keyless from /v1/models on build night,
69 models) shows: DeepSeek/Gemini are premium tier; the 26 free
models are Mistral + Cohere (incl. multilingual Aya Expanse) +
SenseNova. (The Gemini route did NOT dodge this server's region
wall — removed from the walk in build 165.)

Same contract as pipeline/gemini_translate.py: ONE request for ALL
segments, the same mode-aware style prompts (imported, one source),
strict validation; ANY failure raises and the caller falls back
(Apps Script, then the free chain). Since build 165 (owner order)
xKiro sits AFTER OpenRouter in the chain. Build 168 (owner order
2026-11-13): FREE-FIRST — the free models lead the walk below
and the paid DeepSeek pair goes last.

Key handling (security): read ONLY from env XKIRO_API_KEY or the
git-ignored `.env` in the project root; saved via the app's
settings endpoint behind the admin login; never returned, never
logged.
"""
import json
import os
import time
from pathlib import Path

from .common import PROJECT_ROOT
from .gemini_translate import (_extract_json_array, _prompt_for,
                               _validated_result)

_API_URL = "https://api.xkiro.com/v1/chat/completions"

# Free-tier models in the walk, per the live catalog on build
# night — used only for the usage board's free/paid split.
_FREE_MODELS = {"mistralai/mistral-large-2512",
                "cohere/aya-expanse-32b",
                "cohere/command-a-plus"}


def _models():
    """Configured model first, then the FREE tier, the paid
    DeepSeek pair LAST. The free order (build 168): Cohere's
    multilingual Aya Expanse first (the proven winner on this
    server, best format discipline), then Command A+, then
    Mistral Large. A 404/403/402/429 moves to the NEXT model;
    only 401 (bad key) stops the walk.

    Build 168 (owner order 2026-11-13): FREE-FIRST reorder —
    no paid purchase for now, so the free models lead; the
    DeepSeek pair (V4.1 Flash, then V4 Flash 0731) sits at the
    back and is reached only when every free model failed.
    (Build 165 history: Gemini 2.5 Flash was removed from this
    walk — the region wall applies through this gateway too.)"""
    env = (os.environ.get("XKIRO_MODEL") or "").strip()
    out = []
    for m in [env, "cohere/aya-expanse-32b",
              "cohere/command-a-plus",
              "mistralai/mistral-large-2512",
              "deepseek/deepseek-v4.1-flash",
              "deepseek/deepseek-v4-flash-0731"]:
        if m and m not in out:
            out.append(m)
    return out


def _note_model(mdl):
    try:
        (PROJECT_ROOT / ".xkiro_last_model").write_text(
            mdl, encoding="utf-8")
    except Exception:
        pass


LAST_MODEL = None


def last_label():
    """Usage-board label for the most recent success: free-tier
    models and paid models are counted in separate boxes (same
    split the owner ordered for OpenRouter in build 152)."""
    if LAST_MODEL in _FREE_MODELS:
        return "xkiro-free"
    return "xkiro"


def get_api_key():
    key = (os.environ.get("XKIRO_API_KEY") or "").strip()
    if key:
        return key
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("XKIRO_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def key_configured():
    return bool(get_api_key())


def _note_error(msg):
    try:
        (PROJECT_ROOT / ".xkiro_last_error").write_text(
            f"{int(time.time())} {msg}"[:400], encoding="utf-8")
    except Exception:
        pass


def translate_all(segments, style="subs"):
    """segments: list of dicts with 'text'. Returns {index: burmese}.

    Raises on ANY problem so the caller can fall back."""
    import requests

    key = get_api_key()
    if not key:
        raise RuntimeError("no XKIRO_API_KEY configured")
    payload_in = [{"id": i, "text": (s.get("text") or "").strip()}
                  for i, s in enumerate(segments)]
    prompt = _prompt_for(style).replace(
        "__SEGMENTS_JSON__", json.dumps(payload_in, ensure_ascii=False))
    body = {"messages": [{"role": "user", "content": prompt}]}
    attempts = []
    last_body = ""
    for mdl in _models():
        try:
            resp = requests.post(
                _API_URL, json={**body, "model": mdl}, timeout=120,
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"})
        except Exception as e:  # network-level: next model shape
            attempts.append(f"{mdl}=net:{e}"[:60])
            continue
        if resp.status_code == 200:
            # Build 163: validate inside the walk — the live
            # failure that motivated this build was a 200 reply
            # in prose (no JSON array) ending the engine while
            # later models were never tried. The winner is noted
            # only once its reply actually parses.
            try:
                text_out = resp.json()["choices"][0][
                    "message"]["content"]
                result = _validated_result(
                    text_out, segments, "xKiro")
            except Exception as e:
                attempts.append(f"{mdl}=badformat:{e}"[:70])
                continue
            _note_model(mdl)
            globals()["LAST_MODEL"] = mdl
            return result
        attempts.append(f"{mdl}={resp.status_code}")
        last_body = resp.text[:200]
        if resp.status_code == 401:
            break  # bad key — no model will help
        # 402/403/404/429 or anything else: try the next model
        # in the walk.
    msg = ("xKiro failed: " + "; ".join(attempts)
           + f" | last: {last_body}")
    _note_error(msg)
    raise RuntimeError(msg)


if __name__ == "__main__":
    print("xkiro key configured:", key_configured(),
          "| models:", _models())
