"""Orchestrator: link in -> finished .mp4 out, with per-step progress callbacks.

Modes:
  subs  = Myanmar subtitles burned in, original audio + original timing
  dub   = Myanmar voice dub, timing-synced (atempo + freeze-frame extension)
  both  = timing-synced dub AND burned subtitles (re-timed cues)
"""
import shutil
from pathlib import Path

from . import burn, download, fx as fx_mod, retime, transcribe, translate_segments, tts

ALL_STEPS = ["download", "transcribe", "translate", "tts", "retime", "finalize"]


def steps_for_mode(mode):
    if mode == "subs":
        return ["download", "transcribe", "translate", "finalize"]
    return ALL_STEPS


def _marks(progress):
    def mark(step, state, note=""):
        if progress:
            progress(step, state, note)
    return mark


def run_phase1(workdir, url, source_path=None, progress=None):
    """Phase 1: download -> transcribe -> translate. Then the job waits for
    human review/edit of the Myanmar segments before phase 2."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    mark = _marks(progress)

    mark("download", "running")
    if source_path and Path(source_path).exists():
        # preview was already downloaded by /api/preview — reuse that file
        src = workdir / "source.mp4"
        shutil.copyfile(source_path, src)
    else:
        src = download.download(url, workdir)
    mark("download", "done")

    mark("transcribe", "running")
    # Captions first when download.py fetched them (YouTube): parsing a
    # .vtt takes a second, whisper on this CPU takes minutes. Falls back
    # to whisper automatically when no usable captions exist.
    transcribe.transcribe(src, workdir, use_captions=True)
    mark("transcribe", "done")

    mark("translate", "running")
    translate_segments.translate_segments(workdir)
    mark("translate", "done")
    return src


def run_phase2(workdir, mode="both", voice=tts.DEFAULT_VOICE, progress=None,
               fx=None, font=burn.DEFAULT_FONT, voice_rate=0, voice_pitch=0,
               subs=None):
    """Phase 2: from the FINAL (possibly user-edited) segments_my.json —
    TTS -> retime -> logo/blur -> subtitle burn -> output.mp4.
    `subs` is the subtitle-layout dict (size_pct/width_pct/bottom_pct/align);
    None = burn.py defaults (the previous cinematic look)."""
    workdir = Path(workdir)
    mark = _marks(progress)
    src = workdir / "source.mp4"
    final = workdir / "output.mp4"
    if mode == "subs":
        mark("finalize", "running")
        # fx (if any) rides INSIDE the burn encode — no separate fx pass.
        burn.burn(src, workdir / "subs_my.srt", final, font=font, subs=subs,
                  blur_box=(fx or {}).get("blur"),
                  fx=fx if fx_mod.has_fx(fx) else None)
        mark("finalize", "done")
        return final

    mark("tts", "running", voice)
    tts.synthesize(workdir, voice, rate=voice_rate, pitch=voice_pitch)
    mark("tts", "done")

    mark("retime", "running")
    retime.retime(workdir)
    mark("retime", "done")

    mark("finalize", "running")
    base = workdir / "retimed.mp4"
    if mode == "both":
        # fx (if any) rides INSIDE the burn encode — no separate fx pass.
        burn.burn(base, workdir / "subs_retimed.srt", final, font=font,
                  subs=subs, blur_box=(fx or {}).get("blur"),
                  fx=fx if fx_mod.has_fx(fx) else None)
    else:  # dub only — no burn, so fx gets its own (single) pass
        if fx_mod.has_fx(fx):
            fxed = workdir / "fx.mp4"
            fx_mod.apply_effects(base, fxed, fx)
            base = fxed
        shutil.copyfile(base, final)
    mark("finalize", "done")
    return final


def run_job(workdir, url, mode="both", voice=tts.DEFAULT_VOICE, progress=None,
            fx=None, source_path=None, font=burn.DEFAULT_FONT,
            voice_rate=0, voice_pitch=0, subs=None):
    """Uninterrupted run (CLI/back-compat): phase 1 + phase 2, no review."""
    run_phase1(workdir, url, source_path=source_path, progress=progress)
    return run_phase2(workdir, mode=mode, voice=voice, progress=progress,
                      fx=fx, font=font, voice_rate=voice_rate,
                      voice_pitch=voice_pitch, subs=subs)
