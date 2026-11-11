"""OpenRouter translator — build 147 (owner chose this route
2026-11-11 after direct OpenAI proved region-blocked from the VPS).

OpenRouter is an OpenAI-compatible aggregator served from the US:
one key reaches Gemini and GPT models, and it is NOT region-blocked
from the Hong Kong VPS the way api.openai.com is. The wallet is
prepaid via Stripe and purchased credits do not expire.

Same contract as pipeline/gemini_translate.py: ONE request for ALL
segments, the same mode-aware style prompts (imported, one source),
strict validation; ANY failure raises and the caller falls back
(Apps Script, then the free chain).

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
from .gemini_translate import _extract_json_array, _prompt_for

_API_URL = "https://openrouter.ai/api/v1/chat/completions"


def _models():
    """Configured model first, then the name brands, then the
    providers that do not region-block this server, ending with
    FREE variants. Live probe 2026-11-11 (build 150): Google AND
    OpenAI models both answer 403 (region) even via OpenRouter;
    paid DeepSeek answers 402 (wallet empty); the DeepSeek/Qwen/
    Llama :free endpoints I guessed in build 150 are RETIRED
    (404). The live OpenRouter catalog that day lists only 13
    free models; the multilingual chat ones are Google's Gemma 4
    (open-weight, served by third-party hosts, so the Google
    region wall does not apply) and Baidu ERNIE — those are the
    free tail now. A 404/403/402/429 moves to the NEXT model;
    only 401 (bad key) stops the walk."""
    env = (os.environ.get("OPENROUTER_MODEL") or "").strip()
    out = []
    for m in [env, "google/gemini-2.5-flash", "openai/gpt-4o-mini",
              "deepseek/deepseek-chat-v3-0324",
              "google/gemma-4-31b-it:free",
              "google/gemma-4-26b-a4b-it:free",
              "baidu/ernie-5.5-flash-preview:free"]:
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
    data = None
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
            data = resp.json()
            _note_model(mdl)
            globals()["LAST_MODEL"] = mdl
            break
        attempts.append(f"{mdl}={resp.status_code}")
        last_body = resp.text[:200]
        if resp.status_code == 401:
            break  # bad key — no model will help
        # 402 (this model needs credit), 403 (provider region
        # refusal), 404 (model missing), 429 (rate limit) or
        # anything else: try the next model — the free endpoints
        # at the end of the list answer when the paid ones cannot.
    if data is None:
        msg = ("OpenRouter failed: " + "; ".join(attempts)
               + f" | last: {last_body}")
        _note_error(msg)
        raise RuntimeError(msg)
    text_out = data["choices"][0]["message"]["content"]
    arr = _extract_json_array(text_out)
    result = {}
    for item in arr:
        if not isinstance(item, dict) or "id" not in item or "my" not in item:
            raise ValueError("OpenRouter item missing id/my")
        idx, my = int(item["id"]), str(item["my"]).strip()
        if not my:
            raise ValueError("OpenRouter returned an empty translation")
        result[idx] = my
    if set(result) != set(range(len(segments))):
        raise ValueError(
            f"OpenRouter id set mismatch: got {sorted(result)}, "
            f"want 0..{len(segments) - 1}")
    return result


if __name__ == "__main__":
    print("openrouter key configured:", key_configured(),
          "| models:", _models())
