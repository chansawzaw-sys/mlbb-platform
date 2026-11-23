"""Story Video renderer (build 216; quality build 218) —
phase 2 of the Story product: the approved script becomes
a vertical cartoon MP4.

Pipeline: the script is chunked (recap's splitter) and
synthesized through the SAME TTS engines a dub uses (edge-tts
styles, or ElevenLabs for a premium pick — the synthesize()
contracts are shared, so a premium failure fails the job
loudly, never silently re-voiced). The measured per-chunk
speech durations ARE the timeline: shots from the job's plan
are spread across the narration in plan order (build 218:
evenly over the whole timeline, so a shot can change INSIDE
a narration chunk and every planned scene appears), each
shot is composited in two layers from the code-drawn asset
library (assets/story — painted set + a transparent
character layer carrying the shot's expression variants),
animated with a slow ffmpeg zoompan on the set, a gentle
character bob + slide-in on the layer, and fades;
concatenated, muxed with the narration (plus an optional
procedural music bed), and the narration is burned in as
cinematic subtitles through the shared burn pipeline.

Build 218 subtitle fix: the SRT is no longer one cue per
TTS chunk (a ≤450-char cue burned as an ~11-line wall over
the frame — the owner's screenshot). Each chunk's text is
split into sentence/clause-level cues, timed by character
share inside the chunk's measured window, each cue measured
with the burn engine's own font metrics so it wraps to at
most 2 lines at the story burn size (a smaller, lower
profile than the dub default, keeping the text in the
bottom band and the characters visible above it).

Output: 720x1280 (9:16) H.264 + AAC, output.mp4 in the job
dir — the standard finish/download/retention machinery takes
it from there.

Build 220: when the customer ticked the Karaoke option, the
burn runs with style="karaoke" (per-word highlight) — a free
style option; the cue layer, pricing and flow are unchanged.

Build 225 (ANIMATED-STORY-SCOPE.md): shots carrying the
schema-v2 movement vocabulary (per-character entries with
pos/action, a camera move, a fade transition) render
through an animated path — PIL frame compositing at
ANIM_FPS: every character is its own sprite, placed,
faded and walked per frame over a camera-cropped oversized
set, piped into one x264 encode at the house spec. Shots
with NO v2 fields keep the build-218 layered path below,
unchanged; an animated-path failure falls back to it, so
motion never breaks a render.

Build 227: the Story style pack — the job's story_style
(normal / whiteboard / newsreel / anime / pixel) grades
the composited scene frames in BOTH paths (the animated
path grades inside _anim_frame; a styled legacy shot is
composited in PIL by _shot_clip_styled, since the ffmpeg
graph leaves no frame to grade). The grade always lands
before the subtitle burn, so subtitle and karaoke pixels
are never graded; "normal" changes nothing at all.

Build 228: an ElevenLabs story job's voice_rate (the
form's speed slider, stored by create_story for Premium
picks too since 228) is applied to the synthesized
segments with atempo before the timeline is measured —
the same one-speed-pass conversion the Studio uses for
both engines; built-in voices keep taking the rate
natively in their synthesize call.
"""
import math
import os
import re
from pathlib import Path

from . import burn, elevenlabs_tts, gemini_tts, recap as recap_mod, tts
from . import story as story_mod
from .common import (PROJECT_ROOT, encode_threads, load_segments,
                     maxrate_cap_k, probe_duration, run,
                     save_segments)

ASSETS_DIR = PROJECT_ROOT / "assets" / "story"
OUT_W, OUT_H, FPS = 720, 1280, 30
GROUND_MARGIN = 56           # px below the characters' feet
CHAR_HEIGHTS = {"boy": 500, "girl": 500, "man": 520,
                "woman": 500, "elder-man": 500,
                "elder-woman": 500, "buffalo": 350, "dog": 210,
                "cat": 230, "bird": 170, "shadow": 520}
SLOTS = {0: [], 1: [0.5], 2: [0.30, 0.70], 3: [0.20, 0.50, 0.80]}
ANIM_FPS = 12        # build 225: limited-animation frame rate
CAM_SCALE = 1.3      # build 225: the animated set is oversized
                     # by this factor so camera crops have room

_assets_ready = False


def ensure_assets():
    """Materialize the story PNGs from their .b64 companions
    when missing — the fonts pattern (common.ensure_fonts):
    the deploy channel is text-only, the bootstrap decodes
    them too, and this runtime pass makes the renderer
    self-healing either way."""
    global _assets_ready
    if _assets_ready:
        return
    _assets_ready = True
    import base64
    try:
        if not ASSETS_DIR.exists():
            return
        for b64 in ASSETS_DIR.rglob("*.png.b64"):
            png = b64.with_suffix("")
            if not png.exists() or png.stat().st_size < 5_000:
                png.write_bytes(base64.b64decode(
                    b64.read_text(encoding="ascii")))
    except Exception:
        pass


# ---------------- subtitles (build 218) ---------------------
# The story burn profile: smaller and lower than the dub
# default (size 100 / bottom 10), so a 2-line cue block sits
# in the bottom ~15% of the frame and the characters stay
# visible above it. Width/alignment/colour still come from
# the job's own subs settings.
STORY_SUB_SIZE_PCT = 55
STORY_SUB_BOTTOM_PCT = 3
# Hard ceiling per cue; the real gate is the burn engine's
# own line measurement (_cue_fits), this only pre-filters.
CUE_MAX_CHARS = 68
_STORY_FONT_FAMILY = "Noto Sans Myanmar"

_SENT_SPLIT = re.compile(r"(?<=[။!?…])\s*|(?<=\.)\s+")
_CLAUSE_SPLIT = re.compile(r"(?<=[၊])\s*")


def _story_subs(job):
    """The job's subtitle settings with the story profile's
    size/bottom applied (build 218)."""
    subs = burn.normalize_subs((job or {}).get("subs"))
    subs["size_pct"] = STORY_SUB_SIZE_PCT
    subs["bottom_pct"] = STORY_SUB_BOTTOM_PCT
    return subs


def _avail_w(subs):
    width_pct = float((subs or {}).get("width_pct", 95))
    margin_lr = round((100.0 - width_pct) / 200.0 * OUT_W)
    return OUT_W - 2 * margin_lr


def _cue_fits(text, avail_w):
    """True when the cue wraps to at most 2 lines at the
    story burn size, measured with the burn engine's own
    estimator (real font, same correction factors). When
    measuring is impossible the char ceiling alone decides."""
    fs = burn._BASE_FS_FRAC * OUT_H * STORY_SUB_SIZE_PCT / 100.0
    lines = burn._est_lines(text, _STORY_FONT_FAMILY, fs, avail_w)
    return lines is None or lines <= 2


def _force_split(text, avail_w):
    """Build 226 (audit F-06): the last-resort splitter for a
    run no sentence/clause/space boundary can break — a long
    spaceless pasted sentence, common in social-media
    Burmese and heavy script edits. Cut it into the longest
    prefixes that still measure ≤2 lines at the story burn
    size, never cutting right before a combining mark
    (Unicode category M*) — the karaoke unit splitter's rule
    (build 220), so a cut can never break a shaped syllable.
    Cue timings follow automatically: _write_srt times every
    cue by its character share of the chunk window."""
    import unicodedata
    pieces, rest = [], (text or "").strip()
    while rest:
        if len(rest) <= CUE_MAX_CHARS and _cue_fits(rest, avail_w):
            pieces.append(rest)
            break
        # The cut must satisfy BOTH gates at once: the piece
        # measures ≤2 lines AND the next piece does not start
        # with a combining mark (searching downward from the
        # cap — a shorter prefix that ends before the whole
        # syllable keeps both true; extending a fitting cut
        # over its combining marks could tip it back over the
        # line limit, so the boundary moves earlier instead).
        cut = min(len(rest), CUE_MAX_CHARS)
        while cut > 1 and (not _cue_fits(rest[:cut], avail_w)
                           or (cut < len(rest) and unicodedata
                               .category(rest[cut]).startswith("M"))):
            cut -= 1
        pieces.append(rest[:cut])
        rest = rest[cut:].lstrip()
    return pieces


def _split_units(text, avail_w):
    """Sentence-level pieces of a narration chunk, further
    split at clause commas and finally at spaces until every
    piece can stand as (part of) a ≤2-line cue."""
    units = []
    for sent in _SENT_SPLIT.split((text or "").strip()):
        sent = sent.strip()
        if not sent:
            continue
        if len(sent) <= CUE_MAX_CHARS and _cue_fits(sent, avail_w):
            units.append(sent)
            continue
        for clause in _CLAUSE_SPLIT.split(sent):
            clause = clause.strip()
            if not clause:
                continue
            if len(clause) <= CUE_MAX_CHARS \
                    and _cue_fits(clause, avail_w):
                units.append(clause)
                continue
            cur = ""
            for w in clause.split():
                if len(w) > CUE_MAX_CHARS \
                        or not _cue_fits(w, avail_w):
                    # Build 226 (F-06): a single unbreakable
                    # run that cannot stand as a cue on its
                    # own — flush what is packed and force-
                    # split the run instead of letting it
                    # become one wall cue.
                    if cur:
                        units.append(cur)
                        cur = ""
                    units.extend(_force_split(w, avail_w))
                    continue
                trial = (cur + " " + w).strip()
                if cur and (len(trial) > CUE_MAX_CHARS
                            or not _cue_fits(trial, avail_w)):
                    units.append(cur)
                    cur = w
                else:
                    cur = trial
            if cur:
                units.append(cur)
    return units


def split_cues(text, avail_w):
    """Split one narration chunk's text into subtitle cues:
    sentence/clause units greedily packed while the packed
    cue still measures ≤2 lines at the story burn size.
    Only this cue layer changed in build 218 — the TTS
    chunking, the script park and charging are untouched."""
    cues, cur = [], ""
    for u in _split_units(text, avail_w):
        if not cur:
            cur = u
            continue
        trial = cur + " " + u
        if len(trial) <= CUE_MAX_CHARS and _cue_fits(trial, avail_w):
            cur = trial
        else:
            cues.append(cur)
            cur = u
    if cur:
        cues.append(cur)
    return cues


def _apply_el_rate(workdir, rate):
    """Build 228 (story-text audit F4): apply the customer's
    speed-slider rate to ElevenLabs narration. The EL
    synthesize contract carries no rate (the built-in
    engine takes it natively), so — exactly like the
    Studio, which applies ONE atempo pass for both engines
    (build 215) — the rate lands here as an atempo pass
    over each synthesized segment, and the segment
    durations are re-measured so the shot timeline, built
    from those durations, stays in sync. rate is the
    slider's percent (-50..+100) -> factor 0.5..2.0, the
    range a single atempo instance accepts. A zero rate
    touches nothing."""
    try:
        rate = int(rate or 0)
    except (TypeError, ValueError):
        rate = 0
    if not rate:
        return
    factor = min(2.0, max(0.5, 1.0 + rate / 100.0))
    seg_path = Path(workdir) / "segments_my.json"
    segs = load_segments(seg_path)
    changed = False
    for seg in segs:
        f = seg.get("tts_file")
        if not f or not Path(f).exists():
            continue
        tmp = str(f) + ".rate.mp3"
        run(["ffmpeg", "-y", "-v", "error", "-i", str(f),
             "-filter:a", f"atempo={factor:.4f}", tmp],
            timeout=300)
        Path(tmp).replace(f)
        seg["tts_duration"] = round(probe_duration(f), 3)
        changed = True
    if changed:
        save_segments(segs, seg_path)


def synthesize_narration(workdir, job):
    """Chunk the final script, synthesize it with the job's
    voice (the shared dub contracts), and return the spoken
    timeline [(text, seconds, file)] measured from the real
    audio. Raises loudly when nothing was spoken."""
    workdir = Path(workdir)
    text = (job.get("story_text") or "").strip()
    chunks = recap_mod.split_script(text)
    if not chunks:
        raise RuntimeError(
            "Story စာသား မရှိပါ — စာသား ပြန်ထည့်ပြီး ပြန်စမ်းပါ "
            "(credit လုံးဝ မဖြတ်ပါ)")
    total_est = max(8.0, len(text) / recap_mod.CHARS_PER_MIN * 60.0)
    total_chars = sum(len(c) for c in chunks)
    segs, t = [], 0.0
    for c in chunks:
        dur = total_est * len(c) / total_chars
        segs.append({"start": round(t, 3), "end": round(t + dur, 3),
                     "text": "", "text_my": c})
        t += dur
    save_segments(segs, workdir / "segments_my.json")
    if elevenlabs_tts.is_el_voice(job.get("voice")):
        elevenlabs_tts.synthesize(
            workdir, elevenlabs_tts.voice_id_of(job["voice"]),
            voice_name=job.get("voice_name"))
        # Build 228 (audit F4): the slider's rate, applied
        # to the EL segments before their durations are
        # read into the timeline below.
        _apply_el_rate(workdir, job.get("voice_rate", 0))
    elif gemini_tts.is_gm_voice(job.get("voice")):
        # Build 229: a Gemini pick synthesizes through the
        # key pool; the slider's rate rides INSIDE the call
        # (the same atempo pattern, applied by the engine —
        # the Gemini API has no rate parameter).
        gemini_tts.synthesize(
            workdir, gemini_tts.voice_name_of(job["voice"]),
            rate=job.get("voice_rate", 0))
    else:
        tts.synthesize(workdir, job["voice"],
                       rate=job.get("voice_rate", 0),
                       pitch=job.get("voice_pitch", 0))
    spoken = []
    for s in load_segments(workdir / "segments_my.json"):
        dur = float(s.get("tts_duration") or 0)
        if dur > 0 and s.get("tts_file"):
            spoken.append(((s.get("text_my") or "").strip(),
                           dur, s["tts_file"]))
    if not spoken:
        raise RuntimeError(
            "အသံ ထုတ်တာ မရပါ — TTS service တွေ ခဏ အလုပ်မလုပ်ပါ။ "
            "ခဏနေမှ ပြန်စမ်းပါ (credit လုံးဝ မဖြတ်ပါ)")
    return spoken


def _norm_one(s):
    """Normalize ONE stored shot for the renderer (build
    225): valid bg / mood / expr, the cast as entry dicts
    {char, expr, pos, action}, camera + transition, and the
    "animated" flag — True when the STORED shot uses any
    schema-v2 feature (a dict char entry, a camera move, a
    fade transition), which routes it to the animated
    compositing path. A build-216/218 shot (name entries, no
    v2 keys) normalizes with animated=False and keeps the
    legacy render path exactly; its entries inherit the
    shot's expression and stage slots, like build 218.
    Never raises."""
    bg = s.get("bg") if s.get("bg") \
        in story_mod.BACKGROUNDS else "village"
    mood = s.get("mood") if s.get("mood") \
        in story_mod.MOODS else "day"
    expr = s.get("expr") if s.get("expr") \
        in story_mod.EXPRESSIONS else "neutral"
    raw_chars = s.get("chars")
    if not isinstance(raw_chars, list):
        raw_chars = []
    animated = any(isinstance(c, dict) for c in raw_chars) \
        or s.get("camera") not in (None, "none") \
        or s.get("transition") == "fade"
    entries, seen = [], set()
    for c in raw_chars:
        entry = None
        if isinstance(c, dict):
            name = c.get("char", c.get("name"))
            if name in story_mod.CHARACTERS \
                    and name not in seen:
                e_expr = c.get("expr") if c.get("expr") \
                    in story_mod.EXPRESSIONS else expr
                pos = c.get("pos")
                entry = {
                    "char": name, "expr": e_expr,
                    "pos": story_mod.pos_fraction(
                        pos, len(entries), 3)
                    if pos is not None else None,
                    "action": c.get("action") if
                    c.get("action") in story_mod.ACTIONS
                    else "idle"}
        elif isinstance(c, str):
            if c in story_mod.CHARACTERS and c not in seen:
                entry = {"char": c, "expr": expr, "pos": None,
                         "action": "idle" if animated
                         else "slide_in"}
        if entry is None:
            continue
        entries.append(entry)
        seen.add(entry["char"])
        if len(entries) >= story_mod.MAX_CHARS_PER_SHOT:
            break
    n = len(entries)
    for i, e in enumerate(entries):
        if e["pos"] is None:
            slots = SLOTS.get(n) or [0.5]
            e["pos"] = slots[i] if i < len(slots) else 0.5
    return {"bg": bg, "chars": entries, "mood": mood,
            "expr": expr,
            "camera": s.get("camera") if s.get("camera")
            in story_mod.CAMERAS else "none",
            "transition": s.get("transition")
            if s.get("transition") in story_mod.TRANSITIONS
            else "cut",
            "animated": bool(animated)}


def _norm_shots(shots):
    """Normalize a stored plan for the renderer: every shot
    is normalized by _norm_one (legacy build-216 plans have
    no expr — they get "neutral"), and consecutive
    identical pictures are deduped. Never raises, never
    returns an empty list."""
    out = [_norm_one(s) for s in shots or []
           if isinstance(s, dict)]
    out = story_mod.dedupe_shots(out)
    if not out:
        out = [_norm_one(s) for s in story_mod.default_shots()]
    return out


def shot_spans(spoken, shots):
    """Spread the shots across the narration timeline:
    [(shot, start, end)] tiling [0, total] with no gaps.

    Build 218: the shots tile the timeline EVENLY in plan
    order, independent of the TTS chunk boundaries — a
    scene can change inside a narration chunk, and every
    planned shot appears (build 216 cut only at chunk
    boundaries, so a 1-minute video showed ~5 scenes).
    Spans shorter than a second merge into the previous
    shot. This never raises."""
    total = sum(d for _t, d, _f in spoken)
    if total <= 0:
        return [], 0.0
    shots = _norm_shots(shots)
    n = len(shots)
    merged = []
    for i, shot in enumerate(shots):
        a, b = total * i / n, total * (i + 1) / n
        if merged and (b - a) < 1.0:
            merged[-1][2] = b
        else:
            merged.append([shot, a, b])
    if merged:
        merged[-1][2] = total
    return [(s, a, b) for s, a, b in merged], total


def _char_sprite(name, expr):
    """The character's sprite for an expression (build 218):
    <name>-<expr>.png, falling back variant -> neutral ->
    base sprite, so a missing file can never crash a render."""
    ensure_assets()
    cands = []
    if expr and expr != "neutral":
        cands.append(f"{name}-{expr}.png")
    cands += [f"{name}-neutral.png", f"{name}.png"]
    for c in cands:
        p = ASSETS_DIR / "chars" / c
        if p.exists():
            return p
    return ASSETS_DIR / "chars" / f"{name}.png"


def _wash_for(shot):
    """The shot's mood grade (build 218 values): a
    translucent night or sunset wash colour, or None."""
    mood = shot.get("mood")
    if mood == "night" and shot.get("bg") != "night":
        return (10, 16, 48, 110)
    if mood == "sunset":
        return (255, 120, 40, 44)
    return None


def _tint(img, wash):
    """Grade an RGBA image with a mood wash through its own
    alpha (the build-218 technique: opaque areas take the
    full wash, transparent ones none). Mutates and returns
    the image."""
    from PIL import Image
    alpha = img.split()[3]
    tint = Image.new("RGBA", img.size, wash)
    tint.putalpha(alpha.point(lambda v: v * wash[3] // 255))
    img.alpha_composite(tint)
    return img


def _graded_bg(shot):
    """The painted set with the mood wash baked in."""
    from PIL import Image
    ensure_assets()
    bg = Image.open(ASSETS_DIR / "bg" / f"{shot['bg']}.png") \
        .convert("RGBA")
    wash = _wash_for(shot)
    if wash:
        bg.alpha_composite(Image.new("RGBA", bg.size, wash))
    return bg


# ---------------- style pack (build 227) ----------------
# The owner-approved Story looks: a named grade applied to
# the COMPOSITED scene frame in both render paths (the
# build-225 animated PIL path and the build-218 layered
# path), always BEFORE the subtitle burn — the burn runs
# on the finished video afterwards, so subtitle/karaoke
# pixels are never graded. "normal" (or an absent/unknown
# value) keeps the build-226 look exactly: neither path
# calls apply_style at all.
STYLES = ("normal", "whiteboard", "newsreel", "anime",
          "pixel")


def normalize_style(value):
    """The job's stored style, fail-safe to "normal"."""
    v = str(value or "normal").strip().lower()
    return v if v in STYLES else "normal"


def _style_pixel(img):
    """Pixel Game: crush the frame to ~1/6 size and blow
    it back up with NEAREST, so the scene renders in
    chunky blocks; a slight saturation lift keeps it
    gamey."""
    from PIL import Image, ImageEnhance
    w, h = img.size
    small = img.resize((max(1, w // 6), max(1, h // 6)),
                       Image.NEAREST)
    out = small.resize((w, h), Image.NEAREST)
    return ImageEnhance.Color(out).enhance(1.12)


_vignette_masks = {}


def _vignette_mask(size):
    """A soft radial mask: full strength in the centre,
    fading to zero at the corners (cached per size)."""
    from PIL import Image, ImageDraw, ImageFilter
    if size not in _vignette_masks:
        w, h = size
        mask = Image.new("L", size, 0)
        ImageDraw.Draw(mask).ellipse(
            [-w * 0.30, -h * 0.30, w * 1.30, h * 1.30],
            fill=255)
        _vignette_masks[size] = mask.filter(
            ImageFilter.GaussianBlur(radius=max(w, h) * 0.18))
    return _vignette_masks[size]


def _style_newsreel(img, frame_idx=0):
    """1920s Newsreel: monochrome, contrast up, corners
    darkened by a vignette, and a light per-frame grain
    (seeded by the frame index, so it shimmers cheaply at
    12 fps without ever repeating inside a shot)."""
    import random
    from PIL import Image, ImageEnhance, ImageOps
    g = ImageOps.grayscale(img)
    g = ImageEnhance.Contrast(g).enhance(1.28)
    dark = ImageEnhance.Brightness(g).enhance(0.42)
    g = Image.composite(g, dark, _vignette_mask(img.size))
    rgb = g.convert("RGB")
    rng = random.Random(977 + int(frame_idx))
    noise = Image.frombytes("L", img.size,
                            rng.randbytes(img.size[0]
                                          * img.size[1]))
    return Image.blend(rgb, noise.convert("RGB"), 0.05)


def _style_whiteboard(img):
    """Whiteboard: a bright pale grade — a gamma lift and
    brightness up, saturation strongly down, contrast up
    so the characters' dark outlines stay readable. A
    grade approximation, not a redraw."""
    from PIL import ImageEnhance
    out = img.point(lambda v: int(255 * (v / 255) ** 0.82))
    out = ImageEnhance.Color(out).enhance(0.25)
    out = ImageEnhance.Brightness(out).enhance(1.10)
    out = ImageEnhance.Contrast(out).enhance(1.18)
    return out


def _style_anime(img):
    """80s Anime: cel approximation — a slight warm tint,
    saturation pushed up in HSV (per-channel RGB blends
    and the posterize snap itself both drag saturation
    DOWN, measured), then colours posterized to four
    levels per channel so gradients become flat cel
    bands."""
    from PIL import Image
    rgba = img.convert("RGBA")
    _tint(rgba, (255, 130, 50, 26))
    hsv = rgba.convert("RGB").convert("HSV")
    h, s, v = hsv.split()
    s = s.point(lambda x: min(255, int(x * 1.45)))
    out = Image.merge("HSV", (h, s, v)).convert("RGB")
    lut = [(i // 64) * 64 + 32 for i in range(256)]
    return out.point(lut * 3)


def apply_style(img, style, frame_idx=0):
    """Grade ONE composited scene frame (a PIL RGB image)
    with a build-227 style. "normal" returns the frame
    untouched. Callers in both render paths invoke this
    only for a non-normal style, so an unstyled job's
    frames are byte-identical to build 226."""
    if style == "pixel":
        return _style_pixel(img)
    if style == "newsreel":
        return _style_newsreel(img, frame_idx)
    if style == "whiteboard":
        return _style_whiteboard(img)
    if style == "anime":
        return _style_anime(img)
    return img


def _sprite_img(name, expr):
    """A character's expression sprite at its stage height
    (build 218's _char_sprite picks the file)."""
    from PIL import Image
    ch = Image.open(_char_sprite(name, expr))
    h = CHAR_HEIGHTS.get(name, 500)
    w = int(ch.width * h / ch.height)
    return ch.resize((w, h), Image.LANCZOS)


def _char_top(name, height):
    """A sprite's top y on stage: the bird flies; everyone
    else stands on the ground line."""
    if name == "bird":
        return 300
    return OUT_H - GROUND_MARGIN - height


def _shot_layers(shot):
    """Build one shot in two layers: (background, characters).

    The background carries the painted set; the character
    layer is a transparent full-frame canvas with the shot's
    cast at their stage positions (the bird flies), drawn
    from each character's EXPRESSION variant. The mood grade
    (a translucent night or sunset wash) tints the set
    directly and the characters through their own alpha, so
    both layers grade together. Accepts a raw stored shot
    or a normalized one (build 225 entries carry their own
    expr/pos; legacy entries reproduce the build-218 slots
    and shot-level expression exactly)."""
    from PIL import Image
    if "animated" not in shot:
        shot = _norm_one(shot)
    bg = _graded_bg(shot)
    layer = Image.new("RGBA", (OUT_W, OUT_H), (0, 0, 0, 0))
    for e in shot["chars"]:
        spr = _sprite_img(e["char"], e["expr"])
        x = int(e["pos"] * OUT_W - spr.width / 2)
        layer.alpha_composite(
            spr, (x, _char_top(e["char"], spr.height)))
    wash = _wash_for(shot)
    if wash:
        _tint(layer, wash)
    return bg, layer


# ---------------- animated path (build 225) ----------------

def _camera_window(frac, camera, big_w, big_h):
    """The crop window (x, y, w, h) into the oversized set
    at timeline fraction `frac` (0..1) for a camera move.
    The window keeps the set's aspect; zooming shrinks it,
    panning slides its centre. Pure — the proofs measure
    the moves through this AND through rendered frames."""
    if camera == "zoom_in":
        s = 1.0 - 0.22 * frac
    elif camera == "zoom_out":
        s = 0.78 + 0.22 * frac
    else:
        s = 0.86
    cx = 0.50
    if camera == "pan_left":
        cx = 0.60 - 0.20 * frac
    elif camera == "pan_right":
        cx = 0.40 + 0.20 * frac
    w, h = big_w * s, big_h * s
    x = min(max(cx * big_w - w / 2, 0.0), big_w - w)
    y = min(max(0.5 * big_h - h / 2, 0.0), big_h - h)
    return int(x), int(y), int(w), int(h)


def _char_motion(e, spr_w, t, dur):
    """One character's animated state at time t: (centre x,
    alpha 0..1, vertical bob offset in px). idle keeps the
    build-218 gentle bob; slide_in adds the 0.6 s side
    slide; walk enters from the nearest screen edge over
    the first ~60% of the shot with a step bounce, then
    idles; fade_in ramps alpha 0 -> full over the first
    ~40% of the shot (the ghost-reveal move)."""
    pos_x = e["pos"] * OUT_W
    bob = 6 * math.sin(2 * math.pi * t / 2.3)
    action = e.get("action")
    if action == "walk":
        walk_dur = max(0.3, dur * 0.6)
        p = min(t / walk_dur, 1.0) if walk_dur > 0 else 1.0
        pe = 1.0 - (1.0 - p) ** 2
        if pos_x <= OUT_W / 2:
            start_x = -spr_w / 2 - 12
        else:
            start_x = OUT_W + spr_w / 2 + 12
        x = start_x + (pos_x - start_x) * pe
        if p < 1.0:
            bob = -abs(math.sin(2 * math.pi * t / 0.45)) * 8
        return x, 1.0, bob
    if action == "fade_in":
        ramp = max(0.4 * dur, 0.05)
        return pos_x, min(t / ramp, 1.0), bob
    if action == "slide_in":
        p = min(t / 0.6, 1.0)
        d = -34 if pos_x <= OUT_W / 2 else 34
        return pos_x + d * (1.0 - p), 1.0, bob
    return pos_x, 1.0, bob


def _with_alpha(img, factor):
    """A copy of an RGBA sprite with its alpha scaled."""
    a = img.split()[3].point(lambda v: int(v * factor))
    out = img.copy()
    out.putalpha(a)
    return out


def _anim_frame(big, sprites, shot, t, dur, style="normal",
                frame_idx=0):
    """Composite one animated frame: the camera-cropped set
    plus each character at its animated position / alpha.
    `sprites` = [(entry, sprite)] pre-loaded and pre-tinted.
    A "fade" transition fades from/to black at the ends,
    like the legacy clips."""
    from PIL import Image
    frac = min(t / dur, 1.0) if dur > 0 else 0.0
    x, y, w, h = _camera_window(frac, shot.get("camera"),
                                big.width, big.height)
    frame = big.crop((x, y, x + w, y + h)).resize(
        (OUT_W, OUT_H), Image.BILINEAR)
    for e, spr in sprites:
        cx, alpha, bob = _char_motion(e, spr.width, t, dur)
        if alpha <= 0.0:
            continue
        img = spr if alpha >= 0.999 else _with_alpha(spr, alpha)
        top = _char_top(e["char"], spr.height) + int(round(bob))
        frame.alpha_composite(
            img, (int(round(cx - spr.width / 2)), top))
    if shot.get("transition") == "fade" and dur >= 1.0:
        fd = min(0.35, dur / 3.0)
        f = 1.0
        if t < fd:
            f = t / fd
        elif t > dur - fd:
            f = max(0.0, (dur - t) / fd)
        if f < 1.0:
            black = Image.new("RGBA", (OUT_W, OUT_H),
                               (0, 0, 0, 255))
            frame = Image.blend(black, frame, f)
    frame = frame.convert("RGB")
    if style != "normal":
        # Build 227: grade the composited scene frame —
        # the subtitle burn happens later, on the finished
        # video, so subtitle pixels are never graded.
        frame = apply_style(frame, style, frame_idx)
    return frame


def _shot_clip_anim(shot, dur, out_path):
    """Animate one schema-v2 shot (build 225): PIL frame
    compositing at ANIM_FPS — multi-character scenes,
    per-character alpha ramps and camera moves combined in
    one shot — piped as raw frames into one x264 encode at
    the house output spec. Raises on any failure; the
    caller falls back to the build-218 layered clip, so
    motion never breaks a render."""
    import subprocess
    from PIL import Image
    # Build 227: the render loop stamps the job's style
    # onto the normalized shot (never onto the stored
    # plan); absent means the normal look. Reading it here
    # keeps this function's build-225 signature intact.
    style = normalize_style(shot.get("style"))
    bg = _graded_bg(shot)
    big = bg.resize((int(OUT_W * CAM_SCALE),
                     int(OUT_H * CAM_SCALE)), Image.BILINEAR)
    wash = _wash_for(shot)
    sprites = []
    for e in shot["chars"]:
        spr = _sprite_img(e["char"], e["expr"])
        if wash:
            spr = _tint(spr, wash)
        sprites.append((e, spr))
    frames = max(2, int(round(dur * ANIM_FPS)))
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo",
           "-pix_fmt", "rgb24", "-s", f"{OUT_W}x{OUT_H}",
           "-r", str(ANIM_FPS), "-i", "-",
           "-vf", f"fps={FPS},format=yuv420p",
           "-t", f"{dur:.3f}", "-an",
           "-c:v", "libx264", "-preset", "ultrafast",
           "-crf", "24", "-threads", str(encode_threads()),
           str(out_path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        for i in range(frames):
            frame = _anim_frame(big, sprites, shot,
                                i / ANIM_FPS, dur, style=style,
                                frame_idx=i)
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
        if proc.wait(timeout=1200) != 0:
            raise RuntimeError("animated encode failed")
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        raise
    return out_path


def _shot_clip_styled(bg_img, layer_img, dur, idx, out_path,
                      style, still=None):
    """Build 227: the build-218 layered shot with a style
    grade. The ffmpeg graph composites inside the encoder,
    where no per-frame grade can reach, so a styled legacy
    shot is composited in PIL instead — the same zoompan
    curve, layer slide-in and bob, and end fades — with
    apply_style on every composited frame, piped into one
    x264 encode at the house spec (the build-225 pattern).
    Any failure falls back to a clip of the graded still,
    so the style survives even when the motion cannot."""
    import subprocess
    from PIL import Image
    try:
        bg = bg_img.convert("RGB")
        frames = max(2, int(round(dur * ANIM_FPS)))
        if idx % 2 == 0:
            slide, z0, dz = -34.0, 1.0, 0.0011
        else:
            slide, z0, dz = 34.0, 1.11, -0.0011
        fd = min(0.35, dur / 3.0) if dur >= 1.0 else 0.0
        cmd = ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo",
               "-pix_fmt", "rgb24", "-s", f"{OUT_W}x{OUT_H}",
               "-r", str(ANIM_FPS), "-i", "-",
               "-vf", f"fps={FPS},format=yuv420p",
               "-t", f"{dur:.3f}", "-an",
               "-c:v", "libx264", "-preset", "ultrafast",
               "-crf", "24", "-threads", str(encode_threads()),
               str(out_path)]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        try:
            for i in range(frames):
                t = i / ANIM_FPS
                z = z0 + dz * (t * FPS)
                z = min(1.11, z) if dz > 0 else max(1.0, z)
                cw, ch = OUT_W / z, OUT_H / z
                cx, cy = (OUT_W - cw) / 2, (OUT_H - ch) / 2
                frame = bg.crop((int(cx), int(cy),
                                 int(cx + cw),
                                 int(cy + ch))).resize(
                                     (OUT_W, OUT_H),
                                     Image.BILINEAR)
                x = slide * (1.0 - min(t / 0.6, 1.0))
                y = 6 * math.sin(2 * math.pi * t / 2.3)
                frame.paste(layer_img,
                            (int(round(x)), int(round(y))),
                            layer_img)
                if fd:
                    f = 1.0
                    if t < fd:
                        f = t / fd
                    elif t > dur - fd:
                        f = max(0.0, (dur - t) / fd)
                    if f < 1.0:
                        black = Image.new("RGB", (OUT_W, OUT_H),
                                          (0, 0, 0))
                        frame = Image.blend(black, frame, f)
                frame = apply_style(frame, style, i)
                proc.stdin.write(frame.tobytes())
            proc.stdin.close()
            if proc.wait(timeout=1200) != 0:
                raise RuntimeError("styled encode failed")
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
            raise
        return out_path
    except Exception:
        if still is None:
            raise
        return _shot_clip_still(still, dur, idx, out_path)


def _shot_still(shot, out_path):
    """Composite one shot into a single still (the two
    layers flattened) — the fallback render path and the
    proofs' reference image."""
    bg, layer = _shot_layers(shot)
    bg.alpha_composite(layer)
    bg.convert("RGB").save(out_path)
    return out_path


def _shot_clip_still(still, dur, idx, out_path):
    """Animate one composited still (build 216 path, kept
    as the layered clip's fallback): a slow zoom
    (alternating in/out per shot for variety) with short
    fades at both ends."""
    frames = max(1, int(round(dur * FPS)))
    if idx % 2 == 0:
        z = "min(zoom+0.0011,1.11)"
    else:
        z = "if(eq(on,0),1.11,max(zoom-0.0011,1.0))"
    vf = (f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':"
          f"y='ih/2-(ih/zoom/2)':d={frames}:s={OUT_W}x{OUT_H}"
          f":fps={FPS}")
    if dur >= 1.0:
        fd = min(0.35, dur / 3.0)
        vf += (f",fade=t=in:st=0:d={fd:.2f}"
               f",fade=t=out:st={max(0.0, dur - fd):.2f}:d={fd:.2f}")
    vf += ",format=yuv420p"
    # Single-frame input: zoompan stretches that one frame
    # into `frames` output frames (a -loop input would reset
    # the zoom every second).
    run(["ffmpeg", "-y", "-v", "error", "-i",
         str(still), "-vf", vf, "-t", f"{dur:.3f}",
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
         "-threads", str(encode_threads()), str(out_path)],
        timeout=1200)
    return out_path


def _shot_clip(bg_still, layer_png, dur, idx, out_path,
               still=None):
    """Animate one shot (build 218): zoompan on the set as
    before, with the character layer overlaid — the cast
    bobs gently (a small sine lift) and slides in from the
    side over the first 0.6 s of the scene, so the picture
    feels alive without an animation engine. One encode,
    like build 216. Any failure falls back to the build-216
    single-still clip — motion never breaks a render."""
    try:
        frames = max(1, int(round(dur * FPS)))
        if idx % 2 == 0:
            z = "min(zoom+0.0011,1.11)"
            slide = "-34"
        else:
            z = "if(eq(on,0),1.11,max(zoom-0.0011,1.0))"
            slide = "34"
        graph = (
            f"[0:v]zoompan=z='{z}':x='iw/2-(iw/zoom/2)':"
            f"y='ih/2-(ih/zoom/2)':d={frames}:s={OUT_W}x{OUT_H}"
            f":fps={FPS}[bgv];"
            f"[bgv][1:v]overlay="
            f"x='{slide}*(1-min(t/0.6\\,1))':"
            f"y='6*sin(2*PI*t/2.3)':eval=frame")
        if dur >= 1.0:
            fd = min(0.35, dur / 3.0)
            graph += (f",fade=t=in:st=0:d={fd:.2f}"
                      f",fade=t=out:st={max(0.0, dur - fd):.2f}"
                      f":d={fd:.2f}")
        graph += ",format=yuv420p[v]"
        run(["ffmpeg", "-y", "-v", "error", "-i", str(bg_still),
             "-loop", "1", "-framerate", str(FPS),
             "-t", f"{dur:.3f}", "-i", str(layer_png),
             "-filter_complex", graph, "-map", "[v]",
             "-t", f"{dur:.3f}",
             "-c:v", "libx264", "-preset", "ultrafast",
             "-crf", "24", "-threads", str(encode_threads()),
             str(out_path)], timeout=1200)
        return out_path
    except Exception:
        if still is None:
            raise
        return _shot_clip_still(still, dur, idx, out_path)


def _concat(parts, out_path, workdir, audio=False):
    lst = Path(workdir) / ("concat_a.txt" if audio
                           else "concat_v.txt")
    lst.write_text("".join(
        f"file '{Path(p).resolve()}'\n" for p in parts),
        encoding="utf-8")
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "concat",
           "-safe", "0", "-i", str(lst)]
    if audio:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    else:
        cmd += ["-c", "copy"]
    cmd += [str(out_path)]
    run(cmd, timeout=1200)
    return out_path


def _music_bed(dur, out_path):
    """A soft procedural pad (three sine partials of a gentle
    chord through a lowpass with a slow tremolo) — synthesized
    tones only, zero copyrighted material. OFF unless the
    customer asked for it; a failure here drops the music,
    never the job."""
    try:
        fc = ("[0:a][1:a][2:a]amix=inputs=3:normalize=0,"
              "tremolo=f=0.08:d=0.25,lowpass=f=720,volume=0.16,"
              f"afade=t=in:st=0:d=1.5,"
              f"afade=t=out:st={max(0.0, dur - 2.0):.2f}:d=2[a]")
        cmd = ["ffmpeg", "-y", "-v", "error"]
        for f in (196.00, 246.94, 293.66):
            cmd += ["-f", "lavfi", "-i",
                    f"sine=frequency={f}:duration={dur:.2f}"]
        cmd += ["-filter_complex", fc, "-map", "[a]",
                "-c:a", "aac", "-b:a", "96k", str(out_path)]
        run(cmd, timeout=600)
        return out_path if Path(out_path).exists() else None
    except Exception:
        return None


def _srt_time(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _write_srt(spoken, out_path, subs=None):
    """Build 218: each narration chunk becomes several
    short cues (split_cues), each timed by its character
    share of the chunk's measured window — the burned
    result is at most 2 lines per cue instead of one wall
    of text per chunk."""
    avail_w = _avail_w(subs)
    lines, t, idx = [], 0.0, 0
    for text, dur, _f in spoken:
        cues = split_cues(text, avail_w) if text else []
        tot = sum(len(c) for c in cues) or 1
        ct = t
        for c in cues:
            cd = dur * len(c) / tot
            idx += 1
            lines.append(f"{idx}\n{_srt_time(ct)} --> "
                         f"{_srt_time(ct + max(cd, 0.05))}\n"
                         f"{c}\n")
            ct += cd
        t += dur
    Path(out_path).write_text("\n".join(lines), encoding="utf-8")
    return out_path


def render_story(workdir, job, progress=None):
    """Phase 2 end to end: narrate -> compose -> burn ->
    workdir/output.mp4. Progress marks follow the job's step
    keys ("tts", then "render") so timings fold exactly like
    the recap script step's do."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    def mark(step, state):
        if progress:
            progress(step, state)

    mark("tts", "running")
    spoken = synthesize_narration(workdir, job)
    mark("tts", "done")

    mark("render", "running")
    # Build 227: the job's style grade (normal = the
    # build-226 look, neither path grades anything).
    style = normalize_style(job.get("story_style"))
    spans, total = shot_spans(spoken, job.get("story_shots"))
    if not spans:
        raise RuntimeError(
            "Story ဗီဒီယို ဖန်တီးတာ မရပါ — ပြန်စမ်းပါ "
            "(credit လုံးဝ မဖြတ်ပါ)")
    shots_dir = workdir / "story_shots"
    shots_dir.mkdir(exist_ok=True)
    if style != "normal":
        # Build 227: stamp the grade onto the normalized
        # span shots (fresh dicts — the stored plan is
        # never mutated) so the animated path picks it up
        # through its build-225 signature.
        for _shot, _a, _b in spans:
            _shot["style"] = style
    clips = []
    for i, (shot, a, b) in enumerate(spans):
        clip_path = shots_dir / f"clip_{i:03d}.mp4"
        if shot.get("animated"):
            # Build 225: the movement path. Any failure
            # falls through to the build-218 layered clip
            # below — motion never breaks a render.
            try:
                clips.append(_shot_clip_anim(shot, b - a,
                                             clip_path))
                continue
            except Exception:
                pass
        bg_img, layer_img = _shot_layers(shot)
        bg_still = shots_dir / f"shot_{i:03d}_bg.png"
        bg_img.convert("RGB").save(bg_still)
        layer_png = shots_dir / f"shot_{i:03d}_layer.png"
        layer_img.save(layer_png)
        still = _shot_still(shot, shots_dir / f"shot_{i:03d}.png")
        if style != "normal":
            # Build 227: grade the composited frames in PIL
            # (see _shot_clip_styled); the fallback still is
            # graded too, so the look survives a motion
            # failure. The legacy branch below also serves
            # as the animated path's fallback — a styled
            # v2 shot whose animation failed lands here and
            # keeps its style.
            from PIL import Image
            graded = apply_style(
                Image.open(still).convert("RGB"), style)
            graded_still = shots_dir / f"shot_{i:03d}_styled.png"
            graded.save(graded_still)
            clips.append(_shot_clip_styled(
                bg_img, layer_img, b - a, i, clip_path, style,
                still=graded_still))
        else:
            clips.append(_shot_clip(bg_still, layer_png, b - a,
                                    i, clip_path, still=still))
    video = _concat(clips, workdir / "story_video.mp4", workdir)
    narration = _concat([f for _t, _d, f in spoken],
                          workdir / "narration.m4a", workdir,
                          audio=True)
    audio = narration
    if job.get("story_music"):
        bed = _music_bed(total, workdir / "music.m4a")
        if bed:
            mixed = workdir / "story_audio.m4a"
            try:
                run(["ffmpeg", "-y", "-v", "error", "-i",
                     str(narration), "-i", str(bed),
                     "-filter_complex",
                     "[0:a][1:a]amix=inputs=2:duration=first"
                     ":normalize=0[a]",
                     "-map", "[a]", "-c:a", "aac", "-b:a", "128k",
                     str(mixed)], timeout=600)
                audio = mixed
            except Exception:
                audio = narration
    av = workdir / "story_av.mp4"
    run(["ffmpeg", "-y", "-v", "error", "-i", str(video),
         "-i", str(audio), "-map", "0:v", "-map", "1:a",
         "-c:v", "copy", "-c:a", "copy", "-shortest", str(av)],
        timeout=600)
    subs = _story_subs(job)
    srt = _write_srt(spoken, workdir / "story_subs.srt", subs)
    final = workdir / "output.mp4"
    # Build 246: burn to the temp name and rename over output.mp4
    # only when complete, so a kill mid-burn never leaves a torn
    # output.mp4 that a resumed job would mistake for finished.
    tmp_final = workdir / "output.mp4.tmp"
    burn.burn(av, srt, tmp_final, font=job.get("font") or "noto",
              subs=subs, cap_k=maxrate_cap_k("hd"),
              style="karaoke" if job.get("story_karaoke") else None)
    os.replace(tmp_final, final)
    mark("render", "done")
    return final
