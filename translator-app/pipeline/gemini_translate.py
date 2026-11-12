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
import time
from pathlib import Path

from .common import PROJECT_ROOT

DEFAULT_MODEL = "gemini-2.0-flash"
_API_TMPL = "https://generativelanguage.googleapis.com/{ver}/models/{model}:generateContent"

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

# Subtitle style (owner's rule, 2026-11-11, build 142): even for
# subtitle-only jobs the Burmese must read like a STORY being told
# (ဇာတ်လမ်းဆန်ဆန်) — flowing spoken Burmese, connected across
# segments — while staying complete and faithful to what is said.
# (Before build 142, subs used the plain faithful PROMPT above and
# only dub mode had the storyteller voice.)
PROMPT_SUBS = """You are a Burmese storyteller subtitling a video. Render each segment below in natural, spoken Burmese (ပြောစကား) told like a story (ဇာတ်လမ်း ပြောသလို) — smooth and flowing from segment to segment, the way a narrator tells the tale — NOT stiff, word-by-word translation.

Rules:
- Stay COMPLETE and FAITHFUL to what is actually said in the video: translate everything, in order — never add, omit, summarise, soften or censor anything.
- Within that faithfulness, phrase it the way a Burmese storyteller would say it: natural connectors and natural word order, idioms and slang by MEANING never word-by-word, so consecutive segments read like one continuous story and the emotion of the scene carries through.
- FIRST read all the segments together and understand the whole story; then render each segment so it fits the storyline and flows with its neighbours — never translate a segment in isolation.
- NAMES IN ENGLISH: write every personal name, place name and brand name in English (Latin letters) — for a Chinese name use its pinyin/English form; never write a name in Burmese script and never translate a name's meaning. Keep ALL numbers exactly as they are.
- Keep each translation roughly as short as the original (it must fit the same on-screen window).
- Do not add explanations, notes or extra sentences. NO bracketed glosses: never put a parenthetical explanation after a name, a term or any line, and never give a name twice (like "Wang Zhong (Manager Wang)") — every name and term appears ONCE, in its single chosen form, and the translation speaks for itself.

Input is a JSON array of {"id": <int>, "text": "<source segment>"}.
Output MUST be only a JSON array of {"id": <same int>, "my": "<burmese translation>"} with exactly the same ids, in the same order. No markdown fences, no commentary.

Segments:
__SEGMENTS_JSON__
"""

# Dub mode (owner's rule, 2026-11-09): the voice NARRATES — retell the
# scene like a storyteller, in flowing spoken Burmese. Subtitle-only
# jobs keep the faithful style above; anything that will be SPOKEN
# (dub and both) uses this one.
PROMPT_DUB = """You are the narrator of a Burmese-dubbed video. Retell each segment below in natural, spoken Burmese (ပြောစကား) the way a storyteller narrates a story (ဇာတ်လမ်း ပြောသလို) — flowing, vivid, and easy to listen to.

Rules:
- Keep EVERY fact, event and line of meaning, in the same order — never invent, add, omit or soften anything.
- You may rephrase freely and use natural storytelling connectors so the narration flows from segment to segment, as long as the meaning, tone and emotion stay exactly the same.
- FIRST read all the segments together and understand the whole story; then narrate each segment so it fits the storyline and flows with its neighbours — never translate a segment in isolation, never word-by-word.
- When a character speaks, voice it the way that character would say it in Burmese.
- NAMES IN ENGLISH: write every personal name, place name and brand name in English (Latin letters) — for a Chinese name use its pinyin/English form; never write a name in Burmese script and never translate a name's meaning. Keep ALL numbers exactly as they are.
- Keep each segment roughly as short as the original (it will be spoken in the same time window).
- Do not add explanations, notes or extra sentences. NO bracketed glosses: never put a parenthetical explanation after a name, a term or any line, and never give a name twice (like "Wang Zhong (Manager Wang)") — every name and term appears ONCE, in its single chosen form, and the translation speaks for itself.

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


def _models():
    """Model fallback order: the configured model first, then the
    current Flash names. (A key created in late 2026 may not accept
    gemini-2.0-flash at all — the live probe on 2026-11-09 showed
    gemini_ok=false with the single-model client.)"""
    env = (os.environ.get("GEMINI_MODEL") or "").strip()
    out = []
    for m in [env, "gemini-2.5-flash", "gemini-2.0-flash",
              "gemini-1.5-flash"]:
        if m and m not in out:
            out.append(m)
    return out


def _note_error(msg):
    """Record the last Gemini failure (status/body snippet only —
    NEVER the key) so /api/auth/me can surface it for diagnosis."""
    try:
        (PROJECT_ROOT / ".gemini_last_error").write_text(
            f"{int(time.time())} {msg}"[:400], encoding="utf-8")
    except Exception:
        pass


def _extract_json_array(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        raise ValueError("Gemini response contained no JSON array")
    return json.loads(text[start:end + 1])


def _validated_result(text_out, segments, name):
    """Parse one model's reply into {index: burmese} or raise.

    Build 163: every engine calls this INSIDE its model walk, so a
    reply in the wrong shape (prose instead of the JSON array, an
    id missing, an empty string) costs that one attempt and the
    walk moves on to the next model — before 163 the first HTTP 200
    ended the walk and a bad-format reply killed the whole engine
    (live case: xKiro's free Mistral answered 200 with prose while
    Aya Expanse, later in the same list, was never tried)."""
    arr = _extract_json_array(text_out)
    result = {}
    for item in arr:
        if not isinstance(item, dict) or "id" not in item or "my" not in item:
            raise ValueError(f"{name} item missing id/my")
        idx, my = int(item["id"]), str(item["my"]).strip()
        if not my:
            raise ValueError(f"{name} returned an empty translation")
        result[idx] = my
    if set(result) != set(range(len(segments))):
        raise ValueError(
            f"{name} id set mismatch: got {sorted(result)}, "
            f"want 0..{len(segments) - 1}")
    return result


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
    result = None
    attempts = []
    last_body = ""
    for mdl in _models():
        badfmt = False
        for ver in ("v1beta", "v1"):
            url = _API_TMPL.format(ver=ver, model=mdl)
            for use_query in (False, True):
                try:
                    resp = requests.post(
                        url + (f"?key={key}" if use_query else ""),
                        json=body, timeout=120,
                        headers={**({} if use_query else
                                    {"x-goog-api-key": key}),
                                 "Content-Type": "application/json"})
                except Exception as e:  # network-level: next shape
                    attempts.append(f"{mdl}@{ver}=net:{e}"[:60])
                    continue
                if resp.status_code == 200:
                    # Build 163: validate NOW, inside the walk —
                    # a bad-shape reply moves to the next MODEL
                    # (retrying this model under another version
                    # or auth shape would return the same text).
                    try:
                        text_out = resp.json()["candidates"][0][
                            "content"]["parts"][0]["text"]
                        result = _validated_result(
                            text_out, segments, "Gemini")
                    except Exception as e:
                        attempts.append(
                            f"{mdl}@{ver}=badformat:{e}"[:70])
                        badfmt = True
                    break
                # NOTE: body may hold an error message, never the key.
                attempts.append(f"{mdl}@{ver}={resp.status_code}")
                last_body = resp.text[:200]
                if resp.status_code in (400, 404):
                    break  # model/version problem — move on
                if resp.status_code in (401, 403) and not use_query:
                    continue  # try query-param auth once
                break
            if result is not None or badfmt:
                break
        if result is not None:
            break
    if result is None:
        msg = ("Gemini failed: " + "; ".join(attempts)
               + f" | last: {last_body}")
        _note_error(msg)
        raise RuntimeError(msg)
    return result


if __name__ == "__main__":
    print("gemini key configured:", key_configured(),
          "| model:", os.environ.get("GEMINI_MODEL", DEFAULT_MODEL))
