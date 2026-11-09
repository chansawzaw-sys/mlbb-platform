"""Gemini Flash translator — FIRST choice when a key is configured.

Key handling (security): the key is read ONLY from the server-side env var
GEMINI_API_KEY or from the git-ignored `.env` file in the project root.
The user saves it via the app's own settings endpoint (behind the app
login; see app.py) — it never travels through chat, is never returned in
any API payload, and is never written to logs or segment files.

One request translates ALL segments (JSON in/out, ids preserved) with a
prompt that asks for natural spoken Burmese (ပြောစကား), keeping names and
numbers intact and translating idioms by meaning. The response is strictly
validated (same id set, non-empty strings); ANY failure raises, and the
caller falls back to the free Google->Bing->source chain.

Model: env GEMINI_MODEL, default "gemini-2.0-flash" (free tier).

CLI test (mocked in verification; live needs a real key + network):
  python -m pipeline.gemini_translate   # prints whether a key is configured
"""
import json
import os
import re
from pathlib import Path

from .common import PROJECT_ROOT

DEFAULT_MODEL = "gemini-2.0-flash"
_API_TMPL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

PROMPT = """You are a professional video translator. Translate each English subtitle segment below into natural, spoken Burmese (Myanmar) — the way a Myanmar narrator would actually SAY it (ပြောစကား), not stiff written style.

Rules:
- Stay FAITHFUL to what is actually said in the video: translate everything, in order — never add, omit, summarise, soften or censor anything.
- At the same time, render the MEANING in smooth, natural Burmese that fits the scene (အဓိပ္ပာယ် ဆီလျော်အောင်): if a literal rendering sounds awkward, rephrase freely — as long as the meaning, tone and emotion stay exactly the same.
- Translate idioms and slang by MEANING, never word-by-word.
- Keep personal names, place names, brand names and ALL numbers exactly as they are.
- Keep each translation roughly as short as the original (it will be spoken in the same time window).
- Do not add explanations, notes or extra sentences.

Input is a JSON array of {"id": <int>, "text": "<english>"}.
Output MUST be only a JSON array of {"id": <same int>, "my": "<burmese translation>"} with exactly the same ids, in the same order. No markdown fences, no commentary.

Segments:
__SEGMENTS_JSON__
"""

PROMPT_SUBS = PROMPT

# Dub mode (owner's rule, 2026-11-09): the voice NARRATES — retell the
# scene like a storyteller, in flowing spoken Burmese. Subtitle-only
# jobs keep the faithful style above; anything that will be SPOKEN
# (dub and both) uses this one.
PROMPT_DUB = """You are the narrator of a Burmese-dubbed video. Retell each segment below in natural, spoken Burmese (ပြောစကား) the way a storyteller narrates a story (ဇာတ်လမ်း ပြောသလို) — flowing, vivid, and easy to listen to.

Rules:
- Keep EVERY fact, event and line of meaning, in the same order — never invent, add, omit or soften anything.
- You may rephrase freely and use natural storytelling connectors so the narration flows from segment to segment, as long as the meaning, tone and emotion stay exactly the same.
- When a character speaks, voice it the way that character would say it in Burmese.
- Keep personal names, place names, brand names and ALL numbers exactly as they are.
- Keep each segment roughly as short as the original (it will be spoken in the same time window).

Input is a JSON array of {"id": <int>, "text": "<english>"}.
Output MUST be only a JSON array of {"id": <same int>, "my": "<burmese narration>"} with exactly the same ids, in the same order. No markdown fences, no commentary.

Segments:
__SEGMENTS_JSON__
"""


def _prompt_for(style):
    return PROMPT_DUB if style == "dub" else PROMPT_SUBS


def get_api_key():
    key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if key:
        return key
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def key_configured():
    return bool(get_api_key())


def _extract_json_array(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("Gemini response contained no JSON array")
    return json.loads(text[start:end + 1])


def translate_all(segments, style="subs"):
    """segments: list of dicts with 'text'. Returns {index: burmese_text}.

    style: "subs" = faithful + idiomatic (subtitle jobs);
    "dub" = storytelling narration (anything that will be spoken).

    Raises on ANY problem (no key, HTTP error, quota, bad/invalid JSON) so
    the caller can fall back to the free provider chain.
    """
    import requests

    key = get_api_key()
    if not key:
        raise RuntimeError("no GEMINI_API_KEY configured")
    model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    payload_in = [{"id": i, "text": (s.get("text") or "").strip()}
                  for i, s in enumerate(segments)]
    body = {
        "contents": [{"parts": [{"text": _prompt_for(style).replace(
            "__SEGMENTS_JSON__", json.dumps(payload_in, ensure_ascii=False))}]}],
        "generationConfig": {"responseMimeType": "application/json",
                             "temperature": 0.3},
    }
    resp = requests.post(_API_TMPL.format(model=model), json=body, timeout=120,
                         headers={"x-goog-api-key": key,
                                  "Content-Type": "application/json"})
    if resp.status_code != 200:
        # NOTE: body may contain an error message but never the key.
        raise RuntimeError(f"Gemini HTTP {resp.status_code}: {resp.text[:300]}")
    data = resp.json()
    text_out = data["candidates"][0]["content"]["parts"][0]["text"]
    arr = _extract_json_array(text_out)
    result = {}
    for item in arr:
        if not isinstance(item, dict) or "id" not in item or "my" not in item:
            raise ValueError("Gemini item missing id/my")
        idx, my = int(item["id"]), str(item["my"]).strip()
        if not my:
            raise ValueError("Gemini returned an empty translation")
        result[idx] = my
    if set(result) != set(range(len(segments))):
        raise ValueError(
            f"Gemini id set mismatch: got {sorted(result)}, want 0..{len(segments) - 1}")
    return result


if __name__ == "__main__":
    print("gemini key configured:", key_configured(),
          "| model:", os.environ.get("GEMINI_MODEL", DEFAULT_MODEL))
