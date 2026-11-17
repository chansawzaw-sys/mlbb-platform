"""Story Video generation (build 216; shot density +
expression pack build 218; animated shots build 225) — the
Story product's brain: a topic in, a Burmese narration
script + a shot plan out; or the customer's own story text
in, a shot plan out.

Build 225 (ANIMATED-STORY-SCOPE.md): the shot plan grows an
OPTIONAL movement vocabulary — per-character entries
{char, expr, pos, action} (idle / slide_in / walk / fade_in),
a per-shot camera (zoom / pan) and transition (cut / fade),
plus three new assets (the translucent "shadow" character,
the "path" and "interior" sets). sanitize_shots() validates
the new fields with the same strictness as the old ones;
a plan using NONE of them keeps the exact build-218 shape
and renders exactly as build 218 did.

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
               "market", "house", "night", "school", "bridge",
               "path", "interior")
CHARACTERS = ("boy", "girl", "man", "woman", "elder-man",
              "elder-woman", "dog", "cat", "bird", "buffalo",
              "shadow")
MOODS = ("day", "sunset", "night")
EXPRESSIONS = ("neutral", "happy", "sad", "surprised", "angry")
MAX_CHARS_PER_SHOT = 3
# Build 225 movement vocabulary (ANIMATED-STORY-SCOPE.md).
ACTIONS = ("idle", "slide_in", "walk", "fade_in")
CAMERAS = ("none", "zoom_in", "zoom_out", "pan_left",
           "pan_right")
TRANSITIONS = ("cut", "fade")
POS_FRACTIONS = {"left": 0.22, "center": 0.50, "right": 0.78}
# Default stage positions by cast size (mirrors the
# renderer's SLOTS — story.py cannot import story_render).
DEFAULT_POS = {1: (0.50,), 2: (0.30, 0.70),
               3: (0.20, 0.50, 0.80)}


def pos_fraction(pos, idx=0, count=1):
    """Normalize a v2 `pos` to an x fraction of the frame:
    left / center / right words, a number (or numeric
    string) clamped inside the frame, or — for anything
    unusable — the default slot for this cast position.
    Never raises."""
    if isinstance(pos, str):
        if pos in POS_FRACTIONS:
            return POS_FRACTIONS[pos]
        try:
            pos = float(pos)
        except ValueError:
            pos = None
    if isinstance(pos, (int, float)) \
            and not isinstance(pos, bool):
        return min(0.92, max(0.08, float(pos)))
    slots = DEFAULT_POS.get(min(max(count, 1), 3), (0.50,))
    return slots[idx] if idx < len(slots) else 0.50
# Build 218: the owner counted only ~5 scenes in a 1-minute
# build-216 video. The planner now targets ~12 shots per
# minute (a fresh picture every ~5 s) and the sanitizer cap
# scales with the target length instead of one flat cap.
_SHOT_CAPS = {1: 16, 2: 28, 3: 44}
ABSOLUTE_MAX_SHOTS = 48


def max_shots_for_minutes(minutes):
    """Sanitizer cap for a target length: 16 / 28 / 44 shots
    for 1 / 2 / 3 minutes (the planner asks for ~12 per
    minute; the cap leaves headroom, never crashes)."""
    try:
        m = int(round(float(minutes)))
    except (TypeError, ValueError):
        m = 1
    if m in _SHOT_CAPS:
        return _SHOT_CAPS[m]
    return min(ABSOLUTE_MAX_SHOTS, 16 * max(1, m))


def shots_target_for_minutes(minutes):
    """The shot count the planner prompt asks for: ~12 per
    minute of finished video (one scene every ~5 seconds)."""
    try:
        m = float(minutes)
    except (TypeError, ValueError):
        m = 1.0
    return max(10, min(40, int(round(m * 12))))

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
- "shots" is a list of scenes in story order — one shot per story beat. The finished video is about __MINUTES__ minutes long, so give about __SHOTS__ shots (a new scene every 4-6 seconds) so the picture keeps changing.
- Each shot is {"bg": ..., "chars": [...], "mood": ..., "expr": ...} plus the OPTIONAL movement fields below.
- "bg" MUST be exactly one of: village, town, forest, river, field, market, house, night, school, bridge, path, interior
- "chars" is a list of 0 to 3 entries, each exactly one of: boy, girl, man, woman, elder-man, elder-woman, dog, cat, bird, buffalo, shadow
- "mood" is exactly one of: day, sunset, night
- "expr" is exactly one of: neutral, happy, sad, surprised, angry — the main character's feeling during that scene; choose it from what happens in that part of the story.
- Consecutive shots must not repeat the same bg with the same chars — keep changing the scene.
- Use ONLY those words for bg / chars / mood / expr — never invent new ones.

Movement (OPTIONAL — use it on some shots so the cartoon feels alive):
- A "chars" entry may be an object {"char": "boy", "expr": "happy", "pos": "left", "action": "walk"}: "pos" is left / center / right (where the character stands), "action" is one of idle / slide_in / walk / fade_in, and its "expr" may differ per character. Use "walk" when a character enters the scene, "fade_in" when someone or something appears suddenly — above all with "shadow" (a dark ghost figure) appearing behind someone.
- A shot may add "camera": one of zoom_in / zoom_out / pan_left / pan_right for a slow camera move — use it on a few shots only, never on every shot.
- A shot may add "transition": "fade" for a soft change into a very different scene (for example into a night or frightening scene); otherwise leave it out.
- Plain string entries in "chars" are still fine for simple shots.

Answer with ONLY a JSON object, no other text:
{"script": "<the Burmese narration script>", "shots": [{"bg": "village", "chars": ["boy"], "mood": "day", "expr": "happy"}, {"bg": "interior", "chars": [{"char": "girl", "expr": "surprised", "pos": "center", "action": "idle"}, {"char": "shadow", "pos": "right", "action": "fade_in"}], "mood": "night", "expr": "surprised", "camera": "zoom_in", "transition": "fade"}]}

Topic:
__TOPIC__
"""

PROMPT_PLAN = """You are planning cartoon scenes for a Burmese story video. The narration script is below. Make a shot plan: a list of scenes in story order, one shot per story beat — about __SHOTS__ shots (a new scene every 4-6 seconds) so the picture keeps changing.

Each shot is {"bg": ..., "chars": [...], "mood": ..., "expr": ...} plus the OPTIONAL movement fields below.
- "bg" MUST be exactly one of: village, town, forest, river, field, market, house, night, school, bridge, path, interior
- "chars" is a list of 0 to 3 entries, each exactly one of: boy, girl, man, woman, elder-man, elder-woman, dog, cat, bird, buffalo, shadow
- "mood" is exactly one of: day, sunset, night
- "expr" is exactly one of: neutral, happy, sad, surprised, angry — the main character's feeling during that scene; choose it from what happens in that part of the story.
- Consecutive shots must not repeat the same bg with the same chars — keep changing the scene.
- Use ONLY those words — never invent new ones.

Movement (OPTIONAL — use it on some shots so the cartoon feels alive):
- A "chars" entry may be an object {"char": "boy", "expr": "happy", "pos": "left", "action": "walk"}: "pos" is left / center / right (where the character stands), "action" is one of idle / slide_in / walk / fade_in, and its "expr" may differ per character. Use "walk" when a character enters the scene, "fade_in" when someone or something appears suddenly — above all with "shadow" (a dark ghost figure) appearing behind someone.
- A shot may add "camera": one of zoom_in / zoom_out / pan_left / pan_right for a slow camera move — use it on a few shots only, never on every shot.
- A shot may add "transition": "fade" for a soft change into a very different scene (for example into a night or frightening scene); otherwise leave it out.
- Plain string entries in "chars" are still fine for simple shots.

Answer with ONLY a JSON object, no other text:
{"shots": [{"bg": "village", "chars": ["boy"], "mood": "day", "expr": "happy"}, {"bg": "interior", "chars": [{"char": "girl", "expr": "surprised", "pos": "center", "action": "idle"}, {"char": "shadow", "pos": "right", "action": "fade_in"}], "mood": "night", "expr": "surprised", "camera": "zoom_in", "transition": "fade"}]}

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
    failed on every gateway. Build 218: twelve shots (the
    ~12/min density target) with expressions, no two
    consecutive shots alike."""
    return [
        {"bg": "village", "chars": ["boy", "girl"],
         "mood": "day", "expr": "happy"},
        {"bg": "field", "chars": ["boy"], "mood": "day",
         "expr": "neutral"},
        {"bg": "river", "chars": ["girl", "dog"], "mood": "day",
         "expr": "happy"},
        {"bg": "forest", "chars": ["boy", "girl"],
         "mood": "day", "expr": "surprised"},
        {"bg": "bridge", "chars": ["boy"], "mood": "day",
         "expr": "neutral"},
        {"bg": "market", "chars": ["woman", "boy"],
         "mood": "day", "expr": "happy"},
        {"bg": "school", "chars": ["girl"], "mood": "day",
         "expr": "neutral"},
        {"bg": "town", "chars": ["man", "dog"], "mood": "day",
         "expr": "neutral"},
        {"bg": "field", "chars": ["buffalo", "boy"],
         "mood": "sunset", "expr": "happy"},
        {"bg": "house", "chars": ["woman", "girl"],
         "mood": "sunset", "expr": "neutral"},
        {"bg": "village", "chars": ["boy", "cat"],
         "mood": "sunset", "expr": "happy"},
        {"bg": "night", "chars": ["boy"], "mood": "night",
         "expr": "neutral"},
    ]


def dedupe_shots(shots):
    """No two CONSECUTIVE shots may show the same picture
    (same bg AND same character set): a repeat gets its
    background rotated to the next set in the vocabulary.
    Pure and idempotent — the sanitizer applies it to every
    fresh plan and the renderer re-applies it to stored
    plans, so a scene visibly changes every few seconds."""
    out = []
    for s in shots or []:
        s = dict(s)
        if out and s.get("bg") == out[-1].get("bg") \
                and s.get("chars") == out[-1].get("chars"):
            try:
                i = BACKGROUNDS.index(s.get("bg"))
            except ValueError:
                i = -1
            for step in range(1, len(BACKGROUNDS)):
                cand = BACKGROUNDS[(i + step) % len(BACKGROUNDS)]
                if cand != out[-1].get("bg"):
                    s["bg"] = cand
                    break
        out.append(s)
    return out


def _sanitize_chars(item, shot_expr):
    """(chars, extra) for one raw shot (build 225). A shot
    using NO v2 feature returns the build-218 shape — chars
    as a list of names, extra None. A shot using ANY v2
    feature (a dict char entry, a real camera move, a fade
    transition) returns entry dicts {char, expr, pos,
    action} plus {"camera", "transition"}: unknown
    characters are dropped, unknown expr/action/camera/
    transition values fall back to safe defaults, a bad pos
    becomes the default slot, the cast caps at 3. Never
    raises."""
    raw_chars = item.get("characters", item.get("chars"))
    entries = raw_chars if isinstance(raw_chars, list) else []
    camera = item.get("camera")
    transition = item.get("transition")
    is_v2 = any(isinstance(c, dict) for c in entries) \
        or (camera in CAMERAS and camera != "none") \
        or transition == "fade"
    if not is_v2:
        chars = []
        for c in entries:
            if c in CHARACTERS and c not in chars:
                chars.append(c)
            if len(chars) >= MAX_CHARS_PER_SHOT:
                break
        return chars, None
    picked = []
    for c in entries:
        if isinstance(c, dict):
            name, raw_e = c.get("char", c.get("name")), c
        elif isinstance(c, str):
            name, raw_e = c, None
        else:
            continue
        if name not in CHARACTERS:
            continue
        if any(n == name for n, _r in picked):
            continue
        picked.append((name, raw_e))
        if len(picked) >= MAX_CHARS_PER_SHOT:
            break
    chars = []
    for idx, (name, raw_e) in enumerate(picked):
        e = {"char": name, "expr": shot_expr,
             "pos": pos_fraction(None, idx, len(picked)),
             "action": "idle"}
        if raw_e:
            ex = raw_e.get("expr", raw_e.get("expression"))
            if ex in EXPRESSIONS:
                e["expr"] = ex
            pos = raw_e.get("pos", raw_e.get("position"))
            if pos is not None:
                e["pos"] = pos_fraction(pos, idx, len(picked))
            if raw_e.get("action") in ACTIONS:
                e["action"] = raw_e["action"]
        chars.append(e)
    extra = {"camera": camera if camera in CAMERAS else "none",
             "transition": transition
             if transition in TRANSITIONS else "cut"}
    return chars, extra


def sanitize_shots(raw, max_shots=None):
    """Enforce the fixed asset vocabulary on a raw plan.
    Unknown backgrounds fall back to "village", unknown
    characters are dropped, unknown moods become "day",
    unknown/missing expressions become "neutral" (build
    218); non-dict entries are dropped. Build 225: the
    optional movement fields are validated by
    _sanitize_chars with the same strictness, and shots
    that use none of them keep the exact build-218 shape.
    The result is deduped (no two consecutive identical
    pictures). An empty result becomes the default plan —
    this function NEVER raises and NEVER returns an
    empty list."""
    cap = max_shots or ABSOLUTE_MAX_SHOTS
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
            mood = item.get("mood")
            if mood not in MOODS:
                mood = "day"
            expr = item.get("expr", item.get("expression"))
            if expr not in EXPRESSIONS:
                expr = "neutral"
            chars, extra = _sanitize_chars(item, expr)
            shot = {"bg": bg, "chars": chars, "mood": mood,
                    "expr": expr}
            if extra:
                shot.update(extra)
            out.append(shot)
            if len(out) >= cap:
                break
    return dedupe_shots(out) or default_shots()


def generate_story(topic, target_chars):
    """Topic -> (script, shots, provider_label) via the
    gateway chain. Raises RuntimeError with the polite
    Burmese message when every gateway fails or answers
    junk — the caller fails the job (never charged)."""
    minutes = target_chars / CHARS_PER_MIN
    prompt = (PROMPT_STORY
              .replace("__MINUTES__", f"{minutes:.1f}")
              .replace("__SHOTS__",
                       str(shots_target_for_minutes(minutes)))
              .replace("__TARGET__", str(int(target_chars)))
              .replace("__LOW__", str(int(target_chars * 0.8)))
              .replace("__HIGH__", str(int(target_chars * 1.2)))
              .replace("__TOPIC__", topic))

    def extract(raw):
        script, shots_raw = _parse_reply(raw)
        script = (script or "").strip()
        if not _usable_script(script, target_chars):
            return None
        return script, sanitize_shots(
            shots_raw, max_shots_for_minutes(minutes))

    try:
        (script, shots), provider = _chain_generate(prompt, extract)
    except RuntimeError as e:
        raise RuntimeError(FAIL_MSG + f" [{str(e)[:300]}]")
    return script, shots, provider


def plan_for_text(text, minutes=None):
    """Shot plan for a customer-pasted story: (shots,
    provider_label). The script is the customer's own, so a
    total gateway failure falls back to the default plan
    instead of failing their text. `minutes` is the job's
    target length (the shot-density cap follows it); when
    omitted it is estimated from the text length."""
    if not minutes:
        minutes = max(1, int(round(len(text or "")
                                   / CHARS_PER_MIN)))
    prompt = (PROMPT_PLAN
              .replace("__SHOTS__",
                       str(shots_target_for_minutes(minutes)))
              .replace("__SCRIPT__", text[:8000]))

    def extract(raw):
        _script, shots_raw = _parse_reply(raw)
        if not shots_raw:
            return None
        return sanitize_shots(
            shots_raw, max_shots_for_minutes(minutes))

    try:
        shots, provider = _chain_generate(prompt, extract)
        return shots, provider
    except Exception:
        return default_shots(), "default"


def split_script(text, limit=450):
    """TTS chunks for the narration — recap's splitter,
    shared (sentence boundaries first, greedy packing)."""
    return recap.split_script(text, limit)
