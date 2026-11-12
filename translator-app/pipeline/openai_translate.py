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
"""
import json
import os
import time
from pathlib import Path

from .common import PROJECT_ROOT
from .gemini_translate import (_extract_json_array, _prompt_for,
                               _validated_result)

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


def translate_all(segments, style="subs"):
    """segments: list of dicts with 'text'. Returns {index: burmese}.

    Raises on ANY problem so the caller can fall back."""
    import requests

    key = get_api_key()
    if not key:
        raise RuntimeError("no OPENAI_API_KEY configured")
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
            # reply costs this model only, the next one still runs.
            try:
                text_out = resp.json()["choices"][0][
                    "message"]["content"]
                return _validated_result(text_out, segments, "OpenAI")
            except Exception as e:
                attempts.append(f"{mdl}=badformat:{e}"[:70])
                continue
        attempts.append(f"{mdl}={resp.status_code}")
        last_body = resp.text[:200]
        if resp.status_code in (401, 403):
            break  # bad key / no access — other models won't help
        # 404 (model missing) or anything else: try the next model.
    msg = ("OpenAI failed: " + "; ".join(attempts)
           + f" | last: {last_body}")
    _note_error(msg)
    raise RuntimeError(msg)


if __name__ == "__main__":
    print("openai key configured:", key_configured(),
          "| models:", _models())
