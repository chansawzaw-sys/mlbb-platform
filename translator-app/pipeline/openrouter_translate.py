"""OpenRouter translator — build 147 (owner chose this route
2026-11-11 after direct OpenAI proved region-blocked from the VPS).

OpenRouter is an OpenAI-compatible aggregator served from the US:
one key reaches Gemini and GPT models, and it is NOT region-blocked
from the Hong Kong VPS the way api.openai.com is. The wallet is
prepaid via Stripe and purchased credits do not expire.

Same contract as pipeline/gemini_translate.py: ONE request for ALL
segments, the same mode-aware style prompts (imported, one source),
strict validation; ANY failure raises and the caller falls back
(xKiro since build 165, then Apps Script, then the free chain).
Since build 165 (owner order) OpenRouter is the FIRST gateway in
the chain, ahead of xKiro. Build 168 (owner order 2026-11-13):
FREE-FIRST — the credit purchase is deferred, so the free models
lead the walk below and paid DeepSeek goes last. Build 169
(owner order 2026-11-13): BEST-FREE-FIRST — Gemma 4 31B (the
strongest free model in the live catalog) now leads the walk,
with the proven ERNIE as its safety net at #2.

Key handling (security): read ONLY from env OPENROUTER_API_KEY or
the git-ignored `.env` in the project root; saved via the app's
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

_API_URL = "https://openrouter.ai/api/v1/chat/completions"


def _models():
    """Configured model first, then the FREE models, paid
    DeepSeek LAST. The free order (build 169): Google's Gemma 4
    31B first — the strongest free model on the platform
    (frontier-class open weight, explicitly multilingual, and it
    supports response_format/structured outputs like our strict
    JSON contract needs) — then Baidu ERNIE, the proven winner
    on this server, as the safety net at #2, then Gemma 4 26B.
    The Gemma pair is open-weight and third-party hosted, so the
    Google region wall does not apply. A 404/403/402/429 moves
    to the NEXT model; only 401 (bad key) stops the walk.

    Build 169 (owner order 2026-11-13): BEST-FREE-FIRST — the
    walk leads with the strongest free model instead of the
    merely-proven one; ERNIE drops to #2, it does not leave the
    front. (The build-night catalog re-scan also weighed
    nvidia/nemotron-3-ultra-550b:free, a new 550B free entry:
    frontier-sized but a reasoning/orchestration model with NO
    response_format support in its catalog parameters and no
    established multilingual strength — not clearly stronger
    for zh→my translation, so it was NOT promoted.) Build 168
    (owner order 2026-11-13): FREE-FIRST reorder — the owner
    deferred the OpenRouter credit purchase, so the free models
    lead and paid DeepSeek sits at the back; while the wallet
    is empty its 402 is simply walked past, and if the wallet
    is funded later it spends only after every free model has
    failed. (Build 165 history: the Gemini/GPT name brands were
    removed from this walk — 403 region refusals from this
    server every time; the DeepSeek/Qwen/Llama :free endpoints
    guessed in build 150 are retired, 404.)"""
    env = (os.environ.get("OPENROUTER_MODEL") or "").strip()
    out = []
    for m in [env, "google/gemma-4-31b-it:free",
              "baidu/ernie-5.5-flash-preview:free",
              "google/gemma-4-26b-a4b-it:free",
              "deepseek/deepseek-chat-v3-0324"]:
        if m and m not in out:
            out.append(m)
    return out


def _note_model(mdl):
    try:
        (PROJECT_ROOT / ".openrouter_last_model").write_text(
            mdl, encoding="utf-8")
    except Exception:
        pass


LAST_MODEL = None


def last_label():
    """Usage-board label for the most recent success (build 152,
    owner order): videos translated by a FREE endpoint and by a
    PAID model are counted in separate boxes."""
    if LAST_MODEL and LAST_MODEL.endswith(":free"):
        return "openrouter-free"
    return "openrouter"


def get_api_key():
    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if key:
        return key
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def key_configured():
    return bool(get_api_key())


def _note_error(msg):
    try:
        (PROJECT_ROOT / ".openrouter_last_error").write_text(
            f"{int(time.time())} {msg}"[:400], encoding="utf-8")
    except Exception:
        pass


def translate_all(segments, style="subs"):
    """segments: list of dicts with 'text'. Returns {index: burmese}.

    Raises on ANY problem so the caller can fall back."""
    import requests

    key = get_api_key()
    if not key:
        raise RuntimeError("no OPENROUTER_API_KEY configured")
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
            # Build 163: validate inside the walk — a bad-shape
            # reply costs this model only; the winner is noted
            # once its reply actually parses.
            try:
                text_out = resp.json()["choices"][0][
                    "message"]["content"]
                result = _validated_result(
                    text_out, segments, "OpenRouter")
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
        # 402 (this model needs credit), 403 (provider region
        # refusal), 404 (model missing), 429 (rate limit) or
        # anything else: try the next model in the walk.
    msg = ("OpenRouter failed: " + "; ".join(attempts)
           + f" | last: {last_body}")
    _note_error(msg)
    raise RuntimeError(msg)


if __name__ == "__main__":
    print("openrouter key configured:", key_configured(),
          "| models:", _models())
