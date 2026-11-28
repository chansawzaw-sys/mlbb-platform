"""Gemini Flash translator — FIRST choice when a key is configured.

Key handling (security): keys come from the shared pool
(pipeline.gemini_pool, build 229) — the server-side .env's
GEMINI_API_KEYS list, or the legacy GEMINI_API_KEY as a pool of one.
The user saves it via the app's own settings endpoint (behind the app
login; see app.py) — it never travels through chat, is never returned in
any API payload, and is never written to logs or segment files.

One request translates ALL segments (JSON in/out, ids preserved). The
prompt carries the owner's ONE translation rule (build 278: translate
into Myanmar, output only the translation, explain nothing, add
nothing in brackets or parentheses) plus the JSON format scaffolding
— every earlier instruction rule (spoken/storyteller style, names
transliterated into Burmese script by sound, idioms by meaning,
numbers kept exact, no bracketed glosses) was removed on the owner's
order. The response is strictly validated (same id set, non-empty
strings); ANY failure raises, and the caller falls back to the free
Google->Bing->source chain.

Model order: see _models() — the configured model and the static
Flash primary first, then preferred names from the key's LIVE
catalogue, retired names last (build 197).

CLI test (mocked in verification; live needs a real key + network):
  python -m pipeline.gemini_translate   # prints whether a key is configured
"""
import json
import os
import re
import time
from pathlib import Path

from . import gemini_pool, quota, tunnel
from .common import PROJECT_ROOT

DEFAULT_MODEL = "gemini-2.0-flash"
_API_TMPL = "https://generativelanguage.googleapis.com/{ver}/models/{model}:generateContent"

PROMPT = """Translate into Myanmar (Burmese). Output only the translation. Do not explain anything. Do not add anything in brackets or parentheses.

Input is a JSON array of {"id": <int>, "text": "<english>"}.
Output MUST be only a JSON array of {"id": <same int>, "my": "<burmese translation>"} with exactly the same ids, in the same order. No markdown fences, no commentary.

Segments:
__SEGMENTS_JSON__
"""

# Build 278 (owner decision 2026-11-28): ALL instruction rules
# in the translation prompts are stripped — the storyteller
# voice (build 142), the story-first/context instruction
# (build 154), the names-in-Burmese transliteration rule
# (builds 153/252), the no-bracketed-gloss rule (build 154,
# absorbed into the single rule) and the glossary pinning.
# What remains is the owner's ONE rule plus the machine-format
# scaffolding (JSON in/out, one output per input segment, ids
# preserved) the parser depends on.
PROMPT_SUBS = """Translate into Myanmar (Burmese). Output only the translation. Do not explain anything. Do not add anything in brackets or parentheses.

Input is a JSON array of {"id": <int>, "text": "<source segment>"}.
Output MUST be only a JSON array of {"id": <same int>, "my": "<burmese translation>"} with exactly the same ids, in the same order. No markdown fences, no commentary.

Segments:
__SEGMENTS_JSON__
"""

# Dub mode: the same single rule as PROMPT_SUBS (build 278 —
# the storytelling-narration instructions that used to differ
# here are stripped with the rest); only the format
# scaffolding's field labels differ.
PROMPT_DUB = """Translate into Myanmar (Burmese). Output only the translation. Do not explain anything. Do not add anything in brackets or parentheses.

Input is a JSON array of {"id": <int>, "text": "<english>"}.
Output MUST be only a JSON array of {"id": <same int>, "my": "<burmese narration>"} with exactly the same ids, in the same order. No markdown fences, no commentary.

Segments:
__SEGMENTS_JSON__
"""


def _prompt_for(style):
    return PROMPT_DUB if style == "dub" else PROMPT_SUBS


def _build_prompt(style, segments, context=None):
    """The full request prompt for one batch (build 248).

    With context=None this is byte-identical to the pre-248
    construction — the style template with the segments JSON
    substituted in — so a context-free call behaves exactly
    as before. When the caller passes a carry-over context
    (build 248 chunked translation: the story so far for a
    later chunk of a long video), the block is inserted
    immediately before the "Segments:" section, after the
    standing rule, so the block reads as background for the
    segments that follow it. (Build 278: the context no
    longer carries an established-names glossary — the
    story-so-far lines are all that is carried over.)"""
    payload_in = [{"id": i, "text": (s.get("text") or "").strip()}
                  for i, s in enumerate(segments)]
    prompt = _prompt_for(style).replace(
        "__SEGMENTS_JSON__", json.dumps(payload_in,
                                         ensure_ascii=False))
    if context:
        block = str(context).strip()
        if "\nSegments:\n" in prompt:
            prompt = prompt.replace(
                "\nSegments:\n",
                "\n" + block + "\n\nSegments:\n", 1)
        else:  # template drifted: never drop the context
            prompt = prompt + "\n\n" + block
    return prompt


def get_api_key():
    """The pool's first key (build 229: keys live in the
    shared pool now — pipeline.gemini_pool; a lone
    GEMINI_API_KEY still reads as a pool of one). Kept for
    the callers that only need "a" key (diagnostics)."""
    keys = gemini_pool.get_keys()
    return keys[0] if keys else ""


def key_configured():
    return gemini_pool.key_configured()


def list_models():
    """Which models can this key's project actually see? (build 164)

    The 404 wall: generateContent 404s for every walk model while
    the Cloud console shows the Gemini API Enabled on the key's
    project and the project's own metrics record our 404s — the
    requests DO reach the project, so its model catalogue may
    simply differ from our hardcoded names. ListModels is the
    ground truth. Returns (names, error): bare model ids (no
    "models/" prefix) that support generateContent; error "" on
    success."""
    import requests

    keys = gemini_pool.ordered_keys()
    if not keys:
        return [], "no GEMINI_API_KEY configured"
    url = "https://generativelanguage.googleapis.com/v1beta/models"
    last = ""
    # Build 189: ListModels is part of Gemini reachability
    # diagnosis (the app's probe uses it), so it takes the same
    # tunnel as generateContent when the tunnel is verified up.
    # Build 196: kwargs come from the one shared helper, like
    # every Gemini/OpenAI call site.
    # Build 229: the listing walks the key POOL — a quota-dead
    # or invalid key classifies, rotates, and the next key is
    # tried (this is a diagnostic path; trying every key is
    # the informative behaviour).
    for key in keys:
        for use_query in (False, True):
            try:
                resp = requests.get(
                    url + (f"?key={key}" if use_query else ""),
                    **tunnel.request_kwargs(
                        timeout=30,
                        headers={} if use_query else
                        {"x-goog-api-key": key}))
            except Exception as e:
                last = f"net:{e}"[:120]
                continue
            if resp.status_code == 200:
                names = []
                for m in resp.json().get("models") or []:
                    methods = m.get("supportedGenerationMethods") or []
                    if methods and "generateContent" not in methods:
                        continue
                    name = str(m.get("name") or "").replace(
                        "models/", "")
                    if name:
                        names.append(name)
                return names, ""
            last = f"{resp.status_code}: {resp.text[:150]}"
            cls = gemini_pool.classify_error(resp.status_code,
                                             resp.text)
            if cls == "quota_day":
                gemini_pool.mark_cooling_day(key)
                break
            if cls == "quota":
                gemini_pool.mark_cooling(key)
                break
            if cls == "invalid":
                gemini_pool.mark_disabled(key)
                break
    return [], last


# Build 197: the static list alone kept missing the key's real
# catalogue (the live probe returned a full ListModels while every
# generateContent 404'd). The walk now merges in the names the key
# can actually see, in this preference order, after the primaries.
_CATALOGUE_PREFS = ("gemini-flash-latest", "gemini-2.5-flash",
                    "gemini-3.5-flash", "gemini-2.5-flash-lite")
# Retired for new keys (a key created in late 2026 may never have
# had them — the live probe on 2026-11-09 showed gemini_ok=false
# with the single-model client); kept only as last resorts.
_RETIRED_MODELS = ("gemini-2.0-flash", "gemini-1.5-flash")
# A fully-failing walk tries every model x version x auth shape,
# so the model count is capped to keep that walk bounded.
_MAX_MODELS = 6
_CATALOGUE_TTL = 600.0  # 10 min: jobs don't pay a listing per job
_catalogue_cache = {"ts": 0.0, "names": None}  # None = never fetched


def _catalogue_names():
    """The key's live model catalogue (generateContent-capable
    only — list_models already filters), cached briefly at module
    level. A failed listing caches as empty for the same TTL and
    the caller falls back to the static order silently."""
    now = time.time()
    if _catalogue_cache["names"] is not None and \
            now - _catalogue_cache["ts"] < _CATALOGUE_TTL:
        return _catalogue_cache["names"]
    try:
        names, err = list_models()
    except Exception:
        names, err = [], "raise"
    if err:
        names = []
    _catalogue_cache.update(ts=now, names=list(names))
    return _catalogue_cache["names"]


def _models():
    """Model fallback order (build 197): the configured model and
    the static Flash primary first; then the preferred names the
    key's LIVE catalogue actually lists; the retired names last,
    as last resorts. Deduped, capped at _MAX_MODELS."""
    env = (os.environ.get("GEMINI_MODEL") or "").strip()
    out = []
    for m in [env, "gemini-2.5-flash"]:
        if m and m not in out:
            out.append(m)
    catalogue = set(_catalogue_names())
    for m in _CATALOGUE_PREFS:
        if m in catalogue and m not in out:
            out.append(m)
    for m in _RETIRED_MODELS:
        if m not in out:
            out.append(m)
    return out[:_MAX_MODELS]


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


def _walk(key, body, validate):
    """ONE key's full model/version/auth walk (the builds
    163/197 semantics, shared by translate_all and
    generate_text since build 229): returns (result,
    attempts, fail_class). validate(text_out) parses one
    reply or raises — a raise is a bad-format reply: the
    walk moves to the next MODEL (retrying the same model
    under another version or auth shape would return the
    same text). HTTP errors never short-circuit the auth
    loop (build 197: a model/version whose header attempt
    404s may still answer to query-param auth).

    fail_class is the pool's verdict on this key — "quota",
    "invalid" or None (build 229). An "invalid" seen on any
    attempt wins over "quota": the key itself is dead,
    whatever else happened."""
    import requests

    attempts = []
    fail_class = None

    def _clean(text):
        # Diagnostic snippets must never carry the key: an
        # error body shouldn't contain it, but a network
        # exception for a query-auth attempt can embed the
        # full ?key= URL.
        s = str(text or "")
        if key:
            s = s.replace(key, "***")
        return s

    for mdl in _models():
        badfmt = False
        for ver in ("v1beta", "v1"):
            url = _API_TMPL.format(ver=ver, model=mdl)
            for use_query in (False, True):
                auth = "query" if use_query else "header"
                try:
                    resp = requests.post(
                        url + (f"?key={key}" if use_query else ""),
                        **tunnel.request_kwargs(
                            json=body, timeout=120,
                            headers={**({} if use_query else
                                        {"x-goog-api-key": key}),
                                     "Content-Type":
                                         "application/json"}))
                except Exception as e:  # network-level: next shape
                    attempts.append(
                        f"{mdl}@{ver}[{auth}]=net: {_clean(e)[:120]}")
                    continue
                if resp.status_code == 200:
                    # Validate NOW, inside the walk (build
                    # 163): a bad-shape reply moves to the
                    # next MODEL.
                    try:
                        text_out = resp.json()["candidates"][0][
                            "content"]["parts"][0]["text"]
                        return validate(text_out), attempts, None
                    except Exception as e:
                        attempts.append(
                            f"{mdl}@{ver}[{auth}]=badformat: "
                            f"{_clean(e)[:120]}")
                        badfmt = True
                    break
                cls = gemini_pool.classify_error(resp.status_code,
                                                 resp.text)
                if cls == "invalid":
                    fail_class = "invalid"
                elif cls == "quota_day":
                    fail_class = "quota_day"
                elif cls == "quota" and fail_class is None:
                    fail_class = "quota"
                elif cls is None and resp.status_code == 402 \
                        and fail_class is None:
                    # Build 279: a 402 is the prepay wall
                    # (pipeline.quota) — the ACCOUNT's balance
                    # is gone, not a key-shaped failure, so the
                    # pool classifier has no class for it;
                    # record "wall" and let _run_pool turn an
                    # all-wall result into a QuotaWallError.
                    fail_class = "wall"
                # Record EVERY attempt with its own body
                # snippet (build 197).
                attempts.append(
                    f"{mdl}@{ver}[{auth}]={resp.status_code}: "
                    f"{_clean(resp.text)[:120]}")
            if badfmt:
                break
    return None, attempts, fail_class


def _run_pool(body, validate, what):
    """Run one request body across the key pool (build 229):
    a quota failure cools that key down and retries with
    the next; an invalid failure disables that key for good
    and retries with the next; any OTHER failure keeps the
    pre-229 behaviour — stop at once and let the caller
    fall back to the other engines. Raises RuntimeError
    when no key produces a result — or, build 279, a
    quota.QuotaWallError when every key is per-day-dead or
    a 402 prepay wall was seen. Build 189's tunnel rule
    is unchanged: every attempt's kwargs come from
    tunnel.request_kwargs via _walk."""
    keys = gemini_pool.ordered_keys()
    if not keys:
        raise RuntimeError("no GEMINI_API_KEY configured")
    all_attempts = []
    classes = []
    for key in keys:
        result, attempts, fail_class = _walk(key, body, validate)
        all_attempts += attempts
        if result is not None:
            return result
        classes.append(fail_class)
        if fail_class == "quota_day":
            gemini_pool.mark_cooling_day(key)
            continue
        if fail_class == "quota":
            gemini_pool.mark_cooling(key)
            continue
        if fail_class == "invalid":
            gemini_pool.mark_disabled(key)
            continue
        break
    # Build 279 (completing build 275's pool contract for
    # this caller, exactly as gemini_tts already does): when
    # EVERY key landed on the per-day quota, the pool is dead
    # until tomorrow; a 402 anywhere means the prepay balance
    # is gone. Either way the job fails FAST with the quota
    # wall message (zero charge) instead of re-walking dead
    # keys segment after segment.
    if classes and all(c == "quota_day" for c in classes):
        raise quota.QuotaWallError(quota.wall_message(
            "gemini", 429,
            "GenerateRequestsPerDayPerProjectPerModel-FreeTier"))
    if "wall" in classes:
        raise quota.QuotaWallError(
            quota.wall_message("gemini", 402, ""))
    msg = f"Gemini {what}failed: " + "; ".join(all_attempts)
    _note_error(msg)
    raise RuntimeError(msg)


def translate_all(segments, style="subs", context=None):
    """segments: list of dicts with 'text'. Returns {index: burmese_text}.

    style: "subs" = faithful + idiomatic (subtitle jobs);
    "dub" = storytelling narration (anything that will be spoken).

    context (build 248): an optional carry-over block from
    translate_segments' chunked walk — inserted into the
    prompt by _build_prompt; None keeps the pre-248 prompt
    byte for byte.

    Raises on ANY problem (no key, HTTP error, quota, bad/invalid JSON) so
    the caller can fall back to the free provider chain.
    """
    body = {
        "contents": [{"parts": [{"text": _build_prompt(
            style, segments, context)}]}],
        "generationConfig": {"responseMimeType": "application/json",
                             "temperature": 0.3},
    }
    return _run_pool(
        body, lambda t: _validated_result(t, segments, "Gemini"), "")


def generate_text(prompt):
    """Free-form text generation (build 211, for the Recap
    script): ONE generateContent call with the caller's prompt
    over the SAME model/version/auth-shape walk as
    translate_all (tunnel kwargs included), but plain prose
    out — no JSON mime type, no array validation. Returns the
    model's raw text. Raises on ANY problem so the caller can
    fall back."""
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.7},
    }

    def _validate(text_out):
        if not (text_out or "").strip():
            raise ValueError("empty generation")
        return text_out

    return _run_pool(body, _validate, "generation ")



if __name__ == "__main__":
    print("gemini key configured:", key_configured(),
          "| model:", os.environ.get("GEMINI_MODEL", DEFAULT_MODEL))
