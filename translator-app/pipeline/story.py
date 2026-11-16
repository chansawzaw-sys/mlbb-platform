"""Story Video generation (build 216) — the Story product's
brain: a topic in, a Burmese narration script + a shot plan
out; or the customer's own story text in, a shot plan out.

The script is written by the SAME LLM gateways the translator
and Recap use, walked in the SAME order as recap.generate_
script (Gemini leads while its probe verdict is green, then
the free-first gateways OpenRouter -> xKiro, then Gemini's
fallback slot, then OpenAI). Doctrine matches translation and
Recap: if EVERY gateway fails (or answers junk), a topic job
fails loudly and is never charged — there is no silent
fallback script. For a PASTED story the script is the
customer's own words, so only the shot plan is AI work; if
every gateway fails there, a safe built-in plan is used
instead of failing the customer's own text.

The shot plan speaks a FIXED asset vocabulary (the renderer's
code-drawn cast and sets). The prompt embeds the vocabulary
verbatim; sanitize_shots() then enforces it mechanically —
anything outside the lists falls back to safe defaults, so a
creative reply can never invent an asset the renderer lacks,
and can never crash the render.

Sizing mirrors Recap: ~700 Burmese chars per finished minute
(the measured 650-800 chars/min narration band), stated to
the model as a target with a ±20% window.
"""
import json

from . import (gemini_translate, openai_translate,
               openrouter_translate, recap, translate_segments,
               xkiro_translate)

CHARS_PER_MIN = recap.CHARS_PER_MIN

BACKGROUNDS = ("village", "town", "forest", "river", "field",
               "market", "house", "night", "school", "bridge")
CHARACTERS = ("boy", "girl", "man", "woman", "elder-man",
              "elder-woman", "dog", "cat", "bird", "buffalo")
MOODS = ("day", "sunset", "night")
MAX_SHOTS = 24
MAX_CHARS_PER_SHOT = 3

PROMPT_STORY = """You are a Burmese cartoon-story writer. The topic is below. Write a complete short Burmese story narration script for a vertical cartoon video, AND a shot plan for the cartoon scenes.

Rules for the script:
- Tell ONE complete story with a beginning, a middle and an end, in natural spoken Burmese (ပြောစကား), warm storyteller style, easy for children and families to follow.
- NAMES IN ENGLISH: every personal name, place name and brand name stays in Latin letters (Chinese names in pinyin) — never write names in Burmese script, never translate a name by its meaning.
- Numbers must be exact — copy every number precisely.
- STORY-FIRST: plan the whole story first, then write flowing narration — never a list of disconnected lines.
- NO BRACKETED GLOSSES: every name or term appears once, in a single chosen form — no parenthetical explanations, no translator notes.
- Write ONLY the story itself in the "script" field — plain paragraphs of spoken Burmese, ready to be read aloud. No headings, no markdown inside the script.

Length: the finished video is about __MINUTES__ minutes long. The script must be about __TARGET__ Burmese characters (between __LOW__ and __HIGH__) so the narration fills the video at a natural pace.

Shot plan rules:
- "shots" is a list of scenes in story order — one shot per story beat (roughly one shot per 2-4 sentences).
- Each shot is {"bg": ..., "chars": [...], "mood": ...}.
- "bg" MUST be exactly one of: village, town, forest, river, field, market, house, night, school, bridge
- "chars" is a list of 0 to 3 entries, each exactly one of: boy, girl, man, woman, elder-man, elder-woman, dog, cat, bird, buffalo
- "mood" is exactly one of: day, sunset, night
- Use ONLY those words for bg / chars / mood — never invent new ones.

Answer with ONLY a JSON object, no other text:
{"script": "<the Burmese narration script>", "shots": [{"bg": "village", "chars": ["boy"], "mood": "day"}]}

Topic:
__TOPIC__
"""

PROMPT_PLAN = """You are planning cartoon scenes for a Burmese story video. The narration script is below. Make a shot plan: a list of scenes in story order, one shot per story beat (roughly one shot per 2-4 sentences).

Each shot is {"bg": ..., "chars": [...], "mood": ...}.
- "bg" MUST be exactly one of: village, town, forest, river, field, market, house, night, school, bridge
- "chars" is a list of 0 to 3 entries, each exactly one of: boy, girl, man, woman, elder-man, elder-woman, dog, cat, bird, buffalo
- "mood" is exactly one of: day, sunset, night
- Use ONLY those words — never invent new ones.

Answer with ONLY a JSON object, no other text:
{"shots": [{"bg": "village", "chars": ["boy"], "mood": "day"}]}

Script:
__SCRIPT__
"""

FAIL_MSG = ("Story စာသား ရေးတာ မရပါ — AI service တွေ ခဏ "
            "အလုပ်မလုပ်ပါ။ ခဏနေမှ ပြန်စမ်းပါ "
            "(credit လုံးဝ မဖြတ်ပါ)")


def _chain_generate(prompt, extract):
    """Walk the gateways for one generation (recap's order,
    recap's probe seams). Each reply goes through extract();
    the first reply extract() accepts wins — a junk reply
    fails over to the next gateway, exactly like recap's
    in-walk usability gate. Returns (payload, provider_label);
    raises RuntimeError with the per-gateway detail when
    every gateway failed or answered junk."""
    errors = {}

    def _try(name, fn, label_fn):
        try:
            payload = extract(fn(prompt) or "")
            if payload is not None:
                return payload, label_fn()
            errors[name] = "unusable reply"
        except Exception as e:
            errors[name] = str(e)[:100]
        return None

    gemini_tried = False
    if translate_segments._probe_engine_ok("gemini"):
        gemini_tried = True
        if gemini_translate.key_configured():
            r = _try("gemini", gemini_translate.generate_text,
                     lambda: "gemini")
            if r:
                return r
    if openrouter_translate.key_configured():
        r = _try("openrouter", openrouter_translate.generate_text,
                 openrouter_translate.last_label)
        if r:
            return r
    if xkiro_translate.key_configured():
        r = _try("xkiro", xkiro_translate.generate_text,
                 xkiro_translate.last_label)
        if r:
            return r
    if not gemini_tried \
            and not translate_segments._probe_blocked("gemini") \
            and gemini_translate.key_configured():
        r = _try("gemini", gemini_translate.generate_text,
                 lambda: "gemini")
        if r:
            return r
    if not translate_segments._probe_blocked("openai") \
            and openai_translate.key_configured():
        r = _try("openai", openai_translate.generate_text,
                 lambda: "openai")
        if r:
            return r
    detail = "; ".join(f"{k}: {v}" for k, v in errors.items())
    raise RuntimeError(detail[:300] or "no gateway configured")


def _parse_reply(text):
    """(script, raw_shots) from a gateway reply. The reply
    should be one JSON object; be forgiving — strip code
    fences, take the outermost braces. A reply that is not
    JSON at all is treated as a bare script with no plan
    (the sanitizer will supply the default plan)."""
    t = recap._clean(text or "")
    if not t:
        return "", None
    try:
        obj = json.loads(t)
    except Exception:
        a, b = t.find("{"), t.rfind("}")
        obj = None
        if 0 <= a < b:
            try:
                obj = json.loads(t[a:b + 1])
            except Exception:
                obj = None
        if obj is None:
            return t, None
    if not isinstance(obj, dict):
        return "", None
    script = obj.get("script")
    shots = obj.get("shots")
    return (script if isinstance(script, str) else "",
            shots if isinstance(shots, list) else None)


def _usable_script(script, target):
    """Recap's usability bar, shared verbatim: long enough to
    be a real story (a stub reply fails over) and mostly
    Myanmar script (names stay in Latin, so the prose around
    them must carry the ratio)."""
    return recap._usable(script, target)


def default_shots():
    """The safe built-in plan: a gentle day journey through
    the sets with the core cast. Used when a reply carries no
    usable plan, and for a pasted story whose plan generation
    failed on every gateway."""
    return [
        {"bg": "village", "chars": ["boy", "girl"],
         "mood": "day"},
        {"bg": "field", "chars": ["boy"], "mood": "day"},
        {"bg": "river", "chars": ["girl", "dog"], "mood": "day"},
        {"bg": "house", "chars": ["woman"], "mood": "day"},
    ]


def sanitize_shots(raw):
    """Enforce the fixed asset vocabulary on a raw plan.
    Unknown backgrounds fall back to "village", unknown
    characters are dropped, unknown moods become "day";
    non-dict entries are dropped. An empty result becomes
    the default plan — this function NEVER raises and NEVER
    returns an empty list."""
    out = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            # Raw replies use the prompt's key names
            # ("background" / "characters"); the sanitized
            # names are accepted too so re-sanitizing a
            # stored plan is a no-op.
            bg = item.get("background", item.get("bg"))
            if bg not in BACKGROUNDS:
                bg = "village"
            chars = []
            raw_chars = item.get("characters",
                                 item.get("chars"))
            if isinstance(raw_chars, list):
                for c in raw_chars:
                    if c in CHARACTERS and c not in chars:
                        chars.append(c)
                    if len(chars) >= MAX_CHARS_PER_SHOT:
                        break
            mood = item.get("mood")
            if mood not in MOODS:
                mood = "day"
            out.append({"bg": bg, "chars": chars, "mood": mood})
            if len(out) >= MAX_SHOTS:
                break
    return out or default_shots()


def generate_story(topic, target_chars):
    """Topic -> (script, shots, provider_label) via the
    gateway chain. Raises RuntimeError with the polite
    Burmese message when every gateway fails or answers
    junk — the caller fails the job (never charged)."""
    minutes = target_chars / CHARS_PER_MIN
    prompt = (PROMPT_STORY
              .replace("__MINUTES__", f"{minutes:.1f}")
              .replace("__TARGET__", str(int(target_chars)))
              .replace("__LOW__", str(int(target_chars * 0.8)))
              .replace("__HIGH__", str(int(target_chars * 1.2)))
              .replace("__TOPIC__", topic))

    def extract(raw):
        script, shots_raw = _parse_reply(raw)
        script = (script or "").strip()
        if not _usable_script(script, target_chars):
            return None
        return script, sanitize_shots(shots_raw)

    try:
        (script, shots), provider = _chain_generate(prompt, extract)
    except RuntimeError as e:
        raise RuntimeError(FAIL_MSG + f" [{str(e)[:300]}]")
    return script, shots, provider


def plan_for_text(text):
    """Shot plan for a customer-pasted story: (shots,
    provider_label). The script is the customer's own, so a
    total gateway failure falls back to the default plan
    instead of failing their text."""
    prompt = PROMPT_PLAN.replace("__SCRIPT__", text[:8000])

    def extract(raw):
        _script, shots_raw = _parse_reply(raw)
        if not shots_raw:
            return None
        return sanitize_shots(shots_raw)

    try:
        shots, provider = _chain_generate(prompt, extract)
        return shots, provider
    except Exception:
        return default_shots(), "default"


def split_script(text, limit=450):
    """TTS chunks for the narration — recap's splitter,
    shared (sentence boundaries first, greedy packing)."""
    return recap.split_script(text, limit)
