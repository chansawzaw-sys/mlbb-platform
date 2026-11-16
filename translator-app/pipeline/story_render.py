"""Story Video renderer (build 216) — phase 2 of the Story
product: the approved script becomes a vertical cartoon MP4.

Pipeline: the script is chunked (recap's splitter) and
synthesized through the SAME TTS engines a dub uses (edge-tts
styles, or ElevenLabs for a premium pick — the synthesize()
contracts are shared, so a premium failure fails the job
loudly, never silently re-voiced). The measured per-chunk
speech durations ARE the timeline: shots from the job's plan
are spread across the narration, each shot still is composited
from the code-drawn asset library (assets/story — flat 2D
characters over painted sets), animated with a slow ffmpeg
zoompan + fades, concatenated, muxed with the narration (plus
an optional procedural music bed), and the narration is burned
in as cinematic subtitles through the shared burn pipeline.

Output: 720x1280 (9:16) H.264 + AAC, output.mp4 in the job
dir — the standard finish/download/retention machinery takes
it from there.
"""
import math
from pathlib import Path

from . import burn, elevenlabs_tts, recap as recap_mod, tts
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


def shot_spans(spoken, shots):
    """Spread the shots across the narration timeline:
    [(shot, start, end)] tiling [0, total] with no gaps.
    Chunk boundaries are the only clean cuts, so each chunk
    belongs to the shot its start time falls into; spans
    shorter than a second merge into the previous shot. A
    shot the narration never reaches simply does not appear
    — this never raises."""
    total = sum(d for _t, d, _f in spoken)
    if total <= 0:
        return [], 0.0
    shots = shots or [{"bg": "village", "chars": [], "mood": "day"}]
    n = len(shots)
    spans, t = [], 0.0
    for _text, dur, _f in spoken:
        idx = min(n - 1, int(t / total * n))
        if spans and spans[-1][0] == idx:
            spans[-1][2] = t + dur
        else:
            spans.append([idx, t, t + dur])
        t += dur
    merged = []
    for idx, a, b in spans:
        if merged and (b - a) < 1.0:
            merged[-1][2] = b
        else:
            merged.append([idx, a, b])
    if merged:
        merged[-1][2] = total
    return [(shots[i], a, b) for i, a, b in merged], total


def _shot_still(shot, out_path):
    """Composite one shot still: characters layered over the
    background at ground slots (the bird flies), then the
    mood grade — a translucent night or sunset wash (the
    dedicated night set already carries its own grade)."""
    from PIL import Image
    ensure_assets()
    bg = Image.open(ASSETS_DIR / "bg" / f"{shot['bg']}.png") \
        .convert("RGBA")
    chars = (shot.get("chars") or [])[:3]
    for name, fx in zip(chars, SLOTS[len(chars)]):
        ch = Image.open(ASSETS_DIR / "chars" / f"{name}.png")
        h = CHAR_HEIGHTS.get(name, 500)
        w = int(ch.width * h / ch.height)
        ch = ch.resize((w, h), Image.LANCZOS)
        x = int(fx * OUT_W - w / 2)
        if name == "bird":
            y = 300
        else:
            y = OUT_H - GROUND_MARGIN - h
        bg.alpha_composite(ch, (x, y))
    mood = shot.get("mood")
    if mood == "night" and shot["bg"] != "night":
        bg.alpha_composite(Image.new(
            "RGBA", bg.size, (10, 16, 48, 110)))
    elif mood == "sunset":
        bg.alpha_composite(Image.new(
            "RGBA", bg.size, (255, 120, 40, 44)))
    bg.convert("RGB").save(out_path)
    return out_path


def _shot_clip(still, dur, idx, out_path):
    """Animate one still: a slow zoom (alternating in/out per
    shot for variety) with short fades at both ends."""
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


def _write_srt(spoken, out_path):
    lines, t = [], 0.0
    for i, (text, dur, _f) in enumerate(spoken, 1):
        if text:
            lines.append(f"{i}\n{_srt_time(t)} --> "
                         f"{_srt_time(t + dur)}\n{text}\n")
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
        still = _shot_still(shot, shots_dir / f"shot_{i:03d}.png")
        clips.append(_shot_clip(still, b - a, i,
                                shots_dir / f"clip_{i:03d}.mp4"))
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
    srt = _write_srt(spoken, workdir / "story_subs.srt")
    final = workdir / "output.mp4"
    burn.burn(av, srt, final, font=job.get("font") or "noto",
              subs=job.get("subs"), cap_k=maxrate_cap_k("hd"))
    mark("render", "done")
    return final
