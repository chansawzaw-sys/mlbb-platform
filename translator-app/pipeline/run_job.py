"""Orchestrator: link in -> finished .mp4 out, with per-step progress callbacks.

Modes:
  subs  = Myanmar subtitles burned in, original audio + original timing
  dub   = Myanmar voice dub, timing-synced (atempo + freeze-frame extension)
  both  = timing-synced dub AND burned subtitles (re-timed cues)
"""
import shutil
import time
from pathlib import Path

from . import burn, download, elevenlabs_tts, fx as fx_mod, retime, transcribe, translate_segments, tts
from .common import maxrate_cap_k

ALL_STEPS = ["download", "transcribe", "translate", "tts", "retime", "finalize"]


def steps_for_mode(mode):
    if mode == "subs":
        return ["download", "transcribe", "translate", "finalize"]
    return ALL_STEPS


def _marks(progress):
    """Progress marks + per-phase wall-clock timing (build 171).

    Each step's elapsed seconds (its "running" mark to its
    "done" mark) are offered to the progress callback as
    phase_s={step: secs}; app.py stores them on the job dict,
    which is persisted to job.json, so a slow job can be
    diagnosed phase by phase (download / transcribe /
    translate / tts / retime / finalize-encode). Callbacks
    that do not accept the phase_s keyword get the plain call
    instead — timing must never break a job."""
    starts = {}

    def mark(step, state, note=""):
        if not progress:
            return
        if state == "running":
            starts[step] = time.time()
            progress(step, state, note)
            return
        if state == "done" and step in starts:
            secs = round(time.time() - starts.pop(step), 1)
            try:
                progress(step, state, note, phase_s={step: secs})
                return
            except TypeError:
                pass
        progress(step, state, note)
    return mark


def run_phase1(workdir, url, source_path=None, progress=None, style="subs",
               quality="hd"):
    """Phase 1: download -> transcribe -> translate. Then the job waits for
    human review/edit of the Myanmar segments before phase 2.
    `quality` (build 167) is the download rendition choice; the
    caller passes source_path only when the cached preview was
    fetched at this same quality, so a fresh download here honours
    the choice."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    mark = _marks(progress)

    mark("download", "running")
    if source_path and Path(source_path).exists() \
            and Path(source_path).stat().st_size > 50_000:
        # preview was already downloaded by /api/preview — reuse that file.
        # The size gate (build 183) mirrors app.py's _src_valid floor:
        # if the cached preview vanished or was truncated after the
        # job was created (audit A5 — another job's cleanup can remove
        # the shared preview dir), fall back to a normal download
        # instead of copying a stub into the job.
        src = workdir / "source.mp4"
        shutil.copyfile(source_path, src)
        # Build 203 (audit V06): carry the caption sidecars too.
        # download() leaves source.<lang>.vtt next to the preview's
        # source.mp4, and transcribe() looks for them in the JOB
        # workdir — copying only the video stranded them in the
        # preview dir, so every preview-reused job silently fell
        # back to a full Whisper transcription. Stem-matched to
        # the reused source, from its own directory only, so a
        # different video's captions can never leak in. A sidecar
        # that fails to copy (or parses to nothing) is harmless:
        # transcribe() still falls back to Whisper as before.
        _stem = Path(source_path).stem
        for _vtt in sorted(Path(source_path).parent
                           .glob(_stem + ".*.vtt")):
            try:
                shutil.copyfile(_vtt, workdir / _vtt.name)
            except Exception:
                pass
    else:
        src = download.download(url, workdir, quality=quality)
    mark("download", "done")

    mark("transcribe", "running")
    # Captions first when download.py fetched them (YouTube): parsing a
    # .vtt takes a second, whisper on this CPU takes minutes. Falls back
    # to whisper automatically when no usable captions exist.
    transcribe.transcribe(src, workdir, use_captions=True)
    mark("transcribe", "done")

    mark("translate", "running")
    _segs = translate_segments.translate_segments(workdir, style=style)
    _st = translate_segments.translation_stats(_segs or [])
    if _st["total"] >= 4 and _st["untranslated"] * 2 > _st["total"]:
        # Every translation route failed and the source text stood
        # in as a fake 'translation' (owner got a finished video
        # subtitled in Chinese). Fail LOUDLY instead: the job
        # errors, no credit is charged, the customer retries later.
        # D1: append the compact provider breakdown from the job's
        # translate_report.json so the failure says WHICH route
        # failed and why, not just that translation failed.
        _detail = ""
        try:
            import json as _json
            _rep = _json.loads((workdir / "translate_report.json")
                               .read_text(encoding="utf-8"))
            _parts = []
            for _sec in ("batch", "chain", "fill"):
                for _name, _r in (_rep.get(_sec) or {}).items():
                    _ok = _r.get("ok", 0)
                    _bad = _r.get("fail", _r.get("bad", 0))
                    _err = (_r.get("err") or _r.get("last") or "")[:48]
                    if _ok or _bad:
                        _parts.append(f"{_name} {_ok}ok/{_bad}x")
                    if _err:
                        _parts.append(f"{_name}: {_err}")
            if _parts:
                _detail = " [" + "; ".join(_parts[:6]) + "]"
        except Exception:
            pass
        raise RuntimeError(
            "ဘာသာပြန် မရပါ — ဘာသာပြန် service တွေ ခဏ အလုပ်မလုပ်ပါ။ "
            "ခဏနေမှ ပြန်စမ်းပါ (credit လုံးဝ မဖြတ်ပါ)" + _detail)
    # Admin usage board (build 138): count this video under the API
    # that translated most of it. Best-effort — never breaks a job.
    try:
        from . import api_stats
        api_stats.record(_segs or [])
    except Exception:
        pass
    mark("translate", "done")
    return src


def run_phase2(workdir, mode="both", voice=tts.DEFAULT_VOICE, progress=None,
               fx=None, font=burn.DEFAULT_FONT, voice_rate=0, voice_pitch=0,
               subs=None, max_speed=None, quality="hd"):
    """Phase 2: from the FINAL (possibly user-edited) segments_my.json —
    TTS -> retime -> logo/blur -> subtitle burn -> output.mp4.
    `subs` is the subtitle-layout dict (size_pct/width_pct/bottom_pct/align);
    None = burn.py defaults (the previous cinematic look).
    `quality` (build 167) only sets the encode maxrate ceiling via
    maxrate_cap_k — an "hd" job's original rendition is not
    strangled back down by the build-102 cap."""
    workdir = Path(workdir)
    mark = _marks(progress)
    cap_k = maxrate_cap_k(quality)
    src = workdir / "source.mp4"
    final = workdir / "output.mp4"
    if mode == "subs":
        mark("finalize", "running")
        # fx (if any) rides INSIDE the burn encode — no separate fx pass.
        burn.burn(src, workdir / "subs_my.srt", final, font=font, subs=subs,
                  blur_box=(fx or {}).get("blur"),
                  fx=fx if fx_mod.has_fx(fx) else None, cap_k=cap_k)
        mark("finalize", "done")
        return final

    mark("tts", "running", voice)
    if elevenlabs_tts.is_el_voice(voice):
        # Build 205: an ElevenLabs premium pick ("el:<voice_id>")
        # synthesizes through the owner's registry instead of the
        # free edge-tts path. Any failure raises out of here and
        # fails the job loudly — a premium pick is NEVER silently
        # re-voiced with a free voice.
        elevenlabs_tts.synthesize(
            workdir, elevenlabs_tts.voice_id_of(voice))
    else:
        tts.synthesize(workdir, voice, rate=voice_rate, pitch=voice_pitch)
    mark("tts", "done")

    def _make_finalize(plan):
        """Build 182: the finalize graph (fx + subtitle burn for
        mode=both; the fx chain alone for dub-with-fx), rooted at the
        retime concat label so retime can append it to its single-
        pass graph and encode the picture ONCE. Blur pieces are
        remapped through THIS plan first (they were timed on the
        original video). Returns None when there is no finalize
        encode to merge (dub without fx — finalize is a file copy)."""
        fxm = fx
        if fxm and fxm.get("blur_pieces"):
            fxm = {**fxm, "blur_pieces": retime.remap_pieces(
                plan, fxm["blur_pieces"])}
        if mode == "both":
            filters, out_label, logo_path = burn.finalize_filters(
                src, workdir / "subs_retimed.srt", font=font, subs=subs,
                blur_box=(fxm or {}).get("blur"),
                fx=fxm if fx_mod.has_fx(fxm) else None,
                start="[outv]", logo="[2:v]")
            return {"filters": filters, "out_label": out_label,
                    "logo_path": logo_path, "output": final}
        if fx_mod.has_fx(fxm):
            vw, vh = fx_mod.video_size(src)
            filters, cur, logo_path = fx_mod.filter_chain(
                fxm, vw, vh, start="[outv]", logo="[2:v]")
            return {"filters": filters, "out_label": cur,
                    "logo_path": logo_path, "output": final}
        return None

    mark("retime", "running")
    plan = retime.retime(workdir, max_speed=max_speed or retime.MAX_SPEED,
                         cap_k=cap_k, make_finalize=_make_finalize)
    mark("retime", "done")
    if plan.get("final_merged"):
        # The retime encode WAS the finalize encode (build 182) —
        # output.mp4 is already the finished file.
        mark("finalize", "running")
        mark("finalize", "done")
        return final
    if fx and fx.get("blur_pieces"):
        # Blur pieces were timed on the ORIGINAL video; the retime
        # just stretched/shrank the picture, so re-time the windows
        # too (a copy — the job's stored fx keeps source times, so a
        # re-render remaps from the originals again, never twice).
        fx = {**fx, "blur_pieces": retime.remap_pieces(
            plan, fx["blur_pieces"])}

    mark("finalize", "running")
    base = workdir / "retimed.mp4"
    if mode == "both":
        # fx (if any) rides INSIDE the burn encode — no separate fx pass.
        burn.burn(base, workdir / "subs_retimed.srt", final, font=font,
                  subs=subs, blur_box=(fx or {}).get("blur"),
                  fx=fx if fx_mod.has_fx(fx) else None, cap_k=cap_k)
    else:  # dub only — no burn, so fx gets its own (single) pass
        if fx_mod.has_fx(fx):
            fxed = workdir / "fx.mp4"
            fx_mod.apply_effects(base, fxed, fx, cap_k=cap_k)
            base = fxed
        shutil.copyfile(base, final)
    mark("finalize", "done")
    return final


def run_job(workdir, url, mode="both", voice=tts.DEFAULT_VOICE, progress=None,
            fx=None, source_path=None, font=burn.DEFAULT_FONT,
            voice_rate=0, voice_pitch=0, subs=None, quality="hd"):
    """Uninterrupted run (CLI/back-compat): phase 1 + phase 2, no review."""
    run_phase1(workdir, url, source_path=source_path, progress=progress,
               style="subs" if mode == "subs" else "dub", quality=quality)
    return run_phase2(workdir, mode=mode, voice=voice, progress=progress,
                      fx=fx, font=font, voice_rate=voice_rate,
                      voice_pitch=voice_pitch, subs=subs, quality=quality)


def run_recap_phase1(workdir, url, progress=None, quality="hd"):
    """Recap phase 1 (build 211): download -> transcribe. NO
    translation — the recap script is generated from the source
    transcript by pipeline.recap.generate_script (the app's
    recap worker calls it as the job's "script" step), then the
    job parks for the user's review/edit before the render.
    A recap job never reuses a preview download (the Recap view
    has no preview step), so this always fetches fresh through
    the same guarded download chain as a dub job."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    mark = _marks(progress)

    mark("download", "running")
    src = download.download(url, workdir, quality=quality)
    mark("download", "done")

    mark("transcribe", "running")
    # Captions first when download.py fetched them, Whisper
    # otherwise — the same rule as run_phase1.
    transcribe.transcribe(src, workdir, use_captions=True)
    mark("transcribe", "done")
    return src
