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
"""
import re
from pathlib import Path

from . import burn, elevenlabs_tts, recap as recap_mod, tts
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
                "cat": 230, "bird": 170}
SLOTS = {0: [], 1: [0.5], 2: [0.30, 0.70], 3: [0.20, 0.50, 0.80]}

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

_SENT_SPLIT = re.compile(r"(?<=[။!?…])\s*")
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


def _norm_shots(shots):
    """Normalize a stored plan for the renderer: every shot
    gains valid bg / chars / mood / expr (legacy build-216
    plans have no expr — they get "neutral"), unknown values
    fall back safely, and consecutive identical pictures are
    deduped. Never raises, never returns an empty list."""
    out = []
    for s in shots or []:
        if not isinstance(s, dict):
            continue
        out.append({
            "bg": s.get("bg") if s.get("bg")
            in story_mod.BACKGROUNDS else "village",
            "chars": [c for c in (s.get("chars") or [])
                      if c in story_mod.CHARACTERS]
            [:story_mod.MAX_CHARS_PER_SHOT],
            "mood": s.get("mood") if s.get("mood")
            in story_mod.MOODS else "day",
            "expr": s.get("expr") if s.get("expr")
            in story_mod.EXPRESSIONS else "neutral"})
    return story_mod.dedupe_shots(out) \
        or story_mod.default_shots()


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


def _shot_layers(shot):
    """Build one shot in two layers: (background, characters).

    The background carries the painted set; the character
    layer is a transparent full-frame canvas with the shot's
    cast at their ground slots (the bird flies), drawn from
    each character's EXPRESSION variant for this shot. The
    mood grade (a translucent night or sunset wash) tints
    the set directly and the characters through their own
    alpha, so both layers grade together."""
    from PIL import Image
    ensure_assets()
    bg = Image.open(ASSETS_DIR / "bg" / f"{shot['bg']}.png") \
        .convert("RGBA")
    layer = Image.new("RGBA", (OUT_W, OUT_H), (0, 0, 0, 0))
    chars = (shot.get("chars") or [])[:3]
    expr = shot.get("expr") or "neutral"
    for name, fx in zip(chars, SLOTS[len(chars)]):
        ch = Image.open(_char_sprite(name, expr))
        h = CHAR_HEIGHTS.get(name, 500)
        w = int(ch.width * h / ch.height)
        ch = ch.resize((w, h), Image.LANCZOS)
        x = int(fx * OUT_W - w / 2)
        if name == "bird":
            y = 300
        else:
            y = OUT_H - GROUND_MARGIN - h
        layer.alpha_composite(ch, (x, y))
    mood = shot.get("mood")
    wash = None
    if mood == "night" and shot["bg"] != "night":
        wash = (10, 16, 48, 110)
    elif mood == "sunset":
        wash = (255, 120, 40, 44)
    if wash:
        bg.alpha_composite(Image.new("RGBA", bg.size, wash))
        alpha = layer.split()[3]
        tint = Image.new("RGBA", layer.size, wash)
        tint.putalpha(alpha.point(
            lambda v: v * wash[3] // 255))
        layer.alpha_composite(tint)
    return bg, layer


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
    spans, total = shot_spans(spoken, job.get("story_shots"))
    if not spans:
        raise RuntimeError(
            "Story ဗီဒီယို ဖန်တီးတာ မရပါ — ပြန်စမ်းပါ "
            "(credit လုံးဝ မဖြတ်ပါ)")
    shots_dir = workdir / "story_shots"
    shots_dir.mkdir(exist_ok=True)
    clips = []
    for i, (shot, a, b) in enumerate(spans):
        bg_img, layer_img = _shot_layers(shot)
        bg_still = shots_dir / f"shot_{i:03d}_bg.png"
        bg_img.convert("RGB").save(bg_still)
        layer_png = shots_dir / f"shot_{i:03d}_layer.png"
        layer_img.save(layer_png)
        still = _shot_still(shot, shots_dir / f"shot_{i:03d}.png")
        clips.append(_shot_clip(bg_still, layer_png, b - a, i,
                                shots_dir / f"clip_{i:03d}.mp4",
                                still=still))
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
    burn.burn(av, srt, final, font=job.get("font") or "noto",
              subs=subs, cap_k=maxrate_cap_k("hd"))
    mark("render", "done")
    return final
