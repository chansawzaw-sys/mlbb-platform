"""Orchestrator: link in -> finished .mp4 out, with per-step progress callbacks.

Modes:
  subs  = Myanmar subtitles burned in, original audio + original timing
  dub   = Myanmar voice dub, timing-synced (atempo + freeze-frame extension)
  both  = timing-synced dub AND burned subtitles (re-timed cues)
"""
import json
import os
import shutil
import time
from pathlib import Path

from . import burn, download, elevenlabs_tts, fx as fx_mod, gemini_tts, retime, transcribe, translate_segments, tts
from .common import load_segments, maxrate_cap_k, probe_duration

ALL_STEPS = ["download", "transcribe", "translate", "tts", "retime", "finalize"]

# Build 246: restart-proof jobs. Every stage's output lands via a
# temp name + atomic rename where the writer allows it, and the
# gates below decide — from the artifacts on disk alone — whether
# a stage already completed in a previous (killed) attempt. A
# stage counts as done ONLY when its real output artifact exists
# and is complete; anything half-written is redone from the
# stage's start. The final video is encoded to FINAL_TMP_NAME and
# renamed to output.mp4, so a torn final encode never counts.
FINAL_TMP_NAME = "output.mp4.tmp"


def _video_ok(path):
    """True when `path` is an existing, non-empty, probeable video."""
    try:
        path = Path(path)
        return path.exists() and path.stat().st_size > 0 \
            and probe_duration(path) > 0
    except Exception:
        return False


def _load_segments(path):
    try:
        segs = load_segments(Path(path))
        return segs if segs else None
    except Exception:
        return None


def _translate_reuse(workdir):
    """The previous attempt's translation, when it counts as a
    COMPLETED translate stage: segments_my.json parses, its count
    still matches the source segments, the subtitle file exists,
    and the segments would NOT trip the majority-untranslated
    guard (a mid-translate save can hold fallback text — that is
    not a finished translation, the stage runs for real)."""
    my = _load_segments(workdir / "segments_my.json")
    if my is None:
        return None
    if not (workdir / "subs_my.srt").exists():
        return None
    en = _load_segments(workdir / "segments_en.json")
    if en is not None and len(en) != len(my):
        return None
    st = translate_segments.translation_stats(my)
    if st["total"] >= 4 and st["untranslated"] * 2 > st["total"]:
        return None
    return my


def _tts_done(workdir):
    """TTS stage complete ⇔ every segment with text carries a
    synthesized file that still exists AND a tts_text fingerprint
    equal to the segment's CURRENT text — a review edit changes
    the text, mismatches the fingerprint and forces re-synthesis
    of the stage, exactly like a fresh render."""
    segs = _load_segments(workdir / "segments_my.json")
    if not segs:
        return False
    voiced = 0
    for seg in segs:
        text = (seg.get("text_my") or "").strip()
        if not text:
            continue
        voiced += 1
        if seg.get("tts_text") != text:
            return False
        f = seg.get("tts_file")
        if not f:
            return False
        try:
            p = Path(f)
            if not p.exists() or p.stat().st_size <= 0:
                return False
        except Exception:
            return False
    return voiced > 0


def _retime_done(workdir, mode):
    """The persisted retime plan when the retime stage counts as
    complete, else None. A merged-finalize plan counts only when
    the finished output.mp4 itself is valid; a classic plan needs
    retimed.mp4 (plus the re-timed subtitles for mode=both)."""
    try:
        plan = json.loads((workdir / "retime_plan.json")
                          .read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(plan, dict):
        return None
    if plan.get("final_merged"):
        return plan if _video_ok(workdir / "output.mp4") else None
    if not _video_ok(workdir / "retimed.mp4"):
        return None
    if mode == "both" and not (workdir / "subs_retimed.srt").exists():
        return None
    return plan


def steps_for_mode(mode):
    if mode == "subs":
        return ["download", "transcribe", "translate", "finalize"]
    return ALL_STEPS


def _marks(progress):
    """Progress marks + per-phase timing (build 171; build 213:
    measured on the monotonic clock, immune to wall-clock
    jumps).

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
            starts[step] = time.monotonic()
            progress(step, state, note)
            return
        if state == "done" and step in starts:
            secs = round(time.monotonic() - starts.pop(step), 1)
            try:
                progress(step, state, note, phase_s={step: secs})
                return
            except TypeError:
                pass
        progress(step, state, note)
    return mark


def run_phase1(workdir, url, source_path=None, progress=None, style="subs",
               quality="hd", force_source=False, admin=False):
    """Phase 1: download -> transcribe -> translate. Then the job waits for
    human review/edit of the Myanmar segments before phase 2.
    `quality` (build 167) is the download rendition choice; the
    caller passes source_path only when the cached preview was
    fetched at this same quality, so a fresh download here honours
    the choice. `force_source` (build 215) marks a user-UPLOADED
    file: it was already ffprobe-validated at admission, so the
    50 KB preview-integrity floor below does not apply to it.
    `admin` (build 232) is the job account's admin flag — the
    same role lookup that exempts the job from charging —
    and only steers the translation walk order (admin jobs
    lead with Gemini, then OpenAI; see translate_segments)."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    mark = _marks(progress)

    mark("download", "running")
    src = workdir / "source.mp4"
    if _video_ok(src):
        # Build 246 resume: a previous attempt's download already
        # landed complete in this workdir — never fetch it again.
        pass
    elif source_path and Path(source_path).exists() \
            and (force_source
                 or Path(source_path).stat().st_size > 50_000):
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
    # Build 246: an existing, parseable segments_en.json IS the
    # completed transcribe stage — a resumed job never re-transcribes.
    if _load_segments(workdir / "segments_en.json") is None:
        transcribe.transcribe(src, workdir, use_captions=True)
    mark("transcribe", "done")

    mark("translate", "running")
    # Build 246: reuse a completed translation when its artifacts
    # prove it (see _translate_reuse); otherwise translate for real.
    _segs = _translate_reuse(workdir)
    _reused = _segs is not None
    if not _reused:
        _segs = translate_segments.translate_segments(workdir, style=style,
                                                      admin=admin)
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
            for _sec in ("rescue", "fill", "fill2"):
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
    # (A resumed job does not re-count its reused translation.)
    if not _reused:
        try:
            from . import api_stats
            api_stats.record(_segs or [])
        except Exception:
            pass
    mark("translate", "done")
    return src


def run_phase2(workdir, mode="both", voice=tts.DEFAULT_VOICE, progress=None,
               fx=None, font=burn.DEFAULT_FONT, voice_rate=0, voice_pitch=0,
               subs=None, max_speed=None, quality="hd", voice_name=None,
               karaoke=False):
    """Phase 2: from the FINAL (possibly user-edited) segments_my.json —
    TTS -> retime -> logo/blur -> subtitle burn -> output.mp4.
    `subs` is the subtitle-layout dict (size_pct/width_pct/bottom_pct/align);
    None = burn.py defaults (the previous cinematic look).
    `quality` (build 167) only sets the encode maxrate ceiling via
    maxrate_cap_k — an "hd" job's original rendition is not
    strangled back down by the build-102 cap.
    `karaoke` (build 220) applies to every mode that burns
    subtitles ("subs" and "both"): the burn uses the karaoke
    subtitle style — a free style option, same price. Build
    231 extended it from Captions-only to Dub/Recap/
    Commentary as well."""
    workdir = Path(workdir)
    mark = _marks(progress)
    cap_k = maxrate_cap_k(quality)
    src = workdir / "source.mp4"
    final = workdir / "output.mp4"
    tmp_final = workdir / FINAL_TMP_NAME
    # Build 246: a previous attempt may have completed the final
    # encode but died before the atomic rename — a probe-valid
    # temp output is promoted to output.mp4, never re-encoded.
    if not _video_ok(final) and _video_ok(tmp_final):
        os.replace(tmp_final, final)
    if mode == "subs":
        mark("finalize", "running")
        if not _video_ok(final):
            # fx (if any) rides INSIDE the burn encode — no separate
            # fx pass. The burn lands on the temp name and is
            # renamed over output.mp4 only when complete.
            burn.burn(src, workdir / "subs_my.srt", tmp_final, font=font,
                      subs=subs, blur_box=(fx or {}).get("blur"),
                      fx=fx if fx_mod.has_fx(fx) else None, cap_k=cap_k,
                      style="karaoke" if karaoke else None)
            os.replace(tmp_final, final)
        mark("finalize", "done")
        return final

    # Build 246 cascade: a stage may be skipped only when every
    # upstream stage was skipped too — `redo` latches True at the
    # first stage that actually runs and forces everything below.
    redo = False
    _tts_was_done = _tts_done(workdir)
    mark("tts", "running", voice)
    if _tts_was_done:
        pass  # resumed: the synthesized audio is reused as-is
    elif elevenlabs_tts.is_el_voice(voice):
        # Build 205: an ElevenLabs premium pick ("el:<voice_id>")
        # synthesizes through the owner's registry instead of the
        # free edge-tts path. Any failure raises out of here and
        # fails the job loudly — a premium pick is NEVER silently
        # re-voiced with a free voice.
        elevenlabs_tts.synthesize(
            workdir, elevenlabs_tts.voice_id_of(voice),
            voice_name=voice_name)
    elif gemini_tts.is_gm_voice(voice):
        # Build 229: a Gemini pick ("gm:<Name>") synthesizes
        # through the Gemini key pool. Same loud-failure rule
        # as premium: any failure raises and fails the job —
        # never silently re-voiced. (voice_rate is 0 for
        # engine picks in the video products, exactly like
        # "el:"; the rate pass lives in the Story/Studio
        # paths where the slider actually feeds it.)
        gemini_tts.synthesize(
            workdir, gemini_tts.voice_name_of(voice))
    else:
        tts.synthesize(workdir, voice, rate=voice_rate, pitch=voice_pitch)
    if not _tts_was_done:
        redo = True  # the stage ran for real — latch the cascade
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
                start="[outv]", logo="[2:v]",
                style="karaoke" if karaoke else None)
            return {"filters": filters, "out_label": out_label,
                    "logo_path": logo_path, "output": tmp_final}
        if fx_mod.has_fx(fxm):
            vw, vh = fx_mod.video_size(src)
            filters, cur, logo_path = fx_mod.filter_chain(
                fxm, vw, vh, start="[outv]", logo="[2:v]")
            return {"filters": filters, "out_label": cur,
                    "logo_path": logo_path, "output": tmp_final}
        return None

    # Build 246: reuse the persisted plan when the retime stage
    # already completed in a previous attempt (and nothing above
    # it re-ran); otherwise retime for real.
    plan = None if redo else _retime_done(workdir, mode)
    mark("retime", "running")
    if plan is None:
        plan = retime.retime(workdir,
                             max_speed=max_speed or retime.MAX_SPEED,
                             cap_k=cap_k, make_finalize=_make_finalize)
        redo = True
    mark("retime", "done")
    if plan.get("final_merged"):
        # The retime encode WAS the finalize encode (build 182).
        # Build 246: it landed on the temp name — promote it to
        # output.mp4 now that it is complete (a resumed job whose
        # plan was reused already has a valid output.mp4 and no
        # temp file, so this is a no-op there).
        mark("finalize", "running")
        if tmp_final.exists():
            os.replace(tmp_final, final)
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
    if redo or not _video_ok(final):
        # Build 246: the encode/copy lands on the temp name and is
        # renamed over output.mp4 only when complete, so a kill
        # mid-finalize never leaves a torn output.mp4 behind.
        base = workdir / "retimed.mp4"
        if mode == "both":
            # fx (if any) rides INSIDE the burn encode — no separate
            # fx pass.
            burn.burn(base, workdir / "subs_retimed.srt", tmp_final,
                      font=font, subs=subs,
                      blur_box=(fx or {}).get("blur"),
                      fx=fx if fx_mod.has_fx(fx) else None, cap_k=cap_k,
                      style="karaoke" if karaoke else None)
        else:  # dub only — no burn, so fx gets its own (single) pass
            if fx_mod.has_fx(fx):
                fxed = workdir / "fx.mp4"
                fx_mod.apply_effects(base, fxed, fx, cap_k=cap_k)
                base = fxed
            shutil.copyfile(base, tmp_final)
        os.replace(tmp_final, final)
    mark("finalize", "done")
    return final


def run_job(workdir, url, mode="both", voice=tts.DEFAULT_VOICE, progress=None,
            fx=None, source_path=None, font=burn.DEFAULT_FONT,
            voice_rate=0, voice_pitch=0, subs=None, quality="hd",
            admin=False):
    """Uninterrupted run (CLI/back-compat): phase 1 + phase 2, no review."""
    run_phase1(workdir, url, source_path=source_path, progress=progress,
               style="subs" if mode == "subs" else "dub", quality=quality,
               admin=admin)
    return run_phase2(workdir, mode=mode, voice=voice, progress=progress,
                      fx=fx, font=font, voice_rate=voice_rate,
                      voice_pitch=voice_pitch, subs=subs, quality=quality)


def run_recap_phase1(workdir, url, progress=None, quality="hd",
                     source_path=None):
    """Recap phase 1 (build 211): download -> transcribe. NO
    translation — the recap script is generated from the source
    transcript by pipeline.recap.generate_script (the app's
    recap worker calls it as the job's "script" step), then the
    job parks for the user's review/edit before the render.
    A link recap job never reuses a preview download (the Recap
    view has no preview step), so it fetches fresh through the
    same guarded download chain as a dub job. Build 215: an
    UPLOADED source (source_path, already ffprobe-validated at
    admission) is copied in instead and the download is skipped,
    exactly like run_phase1's upload path."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    mark = _marks(progress)

    mark("download", "running")
    src = workdir / "source.mp4"
    if _video_ok(src):
        pass  # build 246 resume: the download already landed
    elif source_path and Path(source_path).exists():
        shutil.copyfile(source_path, src)
    else:
        src = download.download(url, workdir, quality=quality)
    mark("download", "done")

    mark("transcribe", "running")
    # Captions first when download.py fetched them, Whisper
    # otherwise — the same rule as run_phase1. Build 246: an
    # existing segments_en.json is the completed stage.
    if _load_segments(workdir / "segments_en.json") is None:
        transcribe.transcribe(src, workdir, use_captions=True)
    mark("transcribe", "done")
    return src


def run_download(workdir, url, progress=None, quality="hd"):
    """Download-only product (build 234): fetch the source
    through the same guarded chain as every other product and
    STOP there — no transcribe, no translate, no render. The
    deliverable is the downloaded file itself. download() has
    already written source_info.json beside source.mp4; the
    app worker reads the duration from it (ffprobe fallback)
    to quote the per-minute price. NO duration cap here: the
    60-minute ceiling is a Preview-only rule, and this product
    exists precisely for the videos Preview refuses."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    mark = _marks(progress)
    mark("download", "running")
    src = workdir / "source.mp4"
    if not _video_ok(src):
        src = download.download(url, workdir, quality=quality)
    mark("download", "done")
    return src
