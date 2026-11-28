"""Build 260: long-video part mode for phase 2 (dub + captions).

The owner's pain: short videos are fine, but a long video "can't
be done in one go" — build 246 made the STAGES resumable, yet the
media stages themselves are single whole-video passes, so one
failure near the end of (say) an 11-minute render stage redoes
the entire stage. Part mode cuts in at the media stages only —
transcribe and translate stay whole-video (they are already
internally chunked: Groq 600 s windows in build 259, translation
600 s chunks in build 248, and the review park works on the
whole text):

  * When the source runs longer than LONG_PARTS_MIN_SECONDS
    (env setting, default 900 = 15 min), run_phase2 delegates
    here. The source is split into consecutive parts of at most
    PART_SECONDS (600 s); the last part takes the remainder.
  * Each part runs the ORDINARY run_phase2 in its own subdir
    (parts/part_NN/) against a hardlink to the full source, with
    the window passed down (retime trims the window out of the
    full source; the captions burn input-seeks it), the job's
    segments sliced to the window, and the job's exact voice /
    fx / font / karaoke settings. Every build-246 gate inside
    run_phase2 therefore works per part for free.
  * A finished part is copied (temp name + rename, the build-246
    discipline) to part_NN.mp4 in the job dir. A part counts as
    done only when that file probes within tolerance of the
    part's expected duration — on a later attempt completed parts
    are SKIPPED, never re-encoded; the first incomplete part and
    everything after it (re)run, and run_phase2's own gates
    resume inside the part that was interrupted.
  * Finally the parts are concatenated into the job's normal
    output.mp4 (via the finalize temp name + rename). Part
    encodes are parameter-identical by construction (same code
    path, same settings, same source), so the concat is a stream
    copy; the result is probe-validated against the sum of the
    parts and a re-encode fallback covers any parameter drift.

Attempts (build 260): there was deliberately NO extra retry
loop — a part failure failed the job attempt through the
existing honest path (zero charge — charging lives in app.py's
_finish, which an exception never reaches), the completed parts
stayed on disk, and build 246's job-level auto-resume cap (3)
governed how often the job came back.

Build 266 (owner approved 2026-11-27) supersedes that paragraph
— the live evidence: a 2-hour job's stage totals DECREASED
between two progress screenshots (render 23:50 -> 19:25) because
each attempt re-initialised the stage totals and reported only
the parts that ran in THAT attempt, so ~80 minutes of stage work
stretched past 3.5 hours and the owner could not tell progress
from looping; and no part failure recorded its reason anywhere.
Now:

  * A failed part is retried IN PLACE up to PART_MAX_ATTEMPTS
    total attempts (1 + 2 retries) before the failure escalates
    to the same honest job-level path as before. A retry reuses
    the part dir, so run_phase2's own build-246 gates skip the
    part stages that already landed — a retry never re-buys a
    finished stage, and a completed part is still NEVER
    re-encoded. Cancel/shutdown interrupts are never retried.
  * Every failed part attempt appends a structured note to
    part_failures.json in the job dir ({part, stage,
    error_class, short_message, at, attempt}) — the same
    durable store family as the checkpoints, so the notes
    survive restarts. app.py surfaces them to admins only in
    the job views; customers keep the polite messages.
  * Stage totals ACCUMULATE across attempts and resumes in
    parts/accum.json (seeded once from job.json's phase_s for
    a pre-266 job that resumes under this build), so a reported
    stage total can never be lower than a previously reported
    one for the same job.
  * Before each part attempt, a disk floor guard
    (DISK_FLOOR_BYTES) fails the attempt immediately with the
    recorded reason "disk_low" instead of dying obscurely
    mid-encode. Diagnostic only — it never deletes anything.

Segment assignment: a segment belongs to the part containing
its START; its end is clamped to that part's end. A line that
straddles a seam is therefore spoken/shown exactly once (fitted
into the part where it starts) instead of being duplicated into
both parts.
"""
import json
import os
import shutil
import time
from pathlib import Path

from . import cue_timing
from .common import (ENCODE_CRF, PROJECT_ROOT, load_segments,
                     probe_duration, probe_streams, run,
                     save_segments, write_srt)

PART_SECONDS = 600
# Build 266: total attempts per part within one part-mode run
# (the first try plus two in-place retries) before the failure
# escalates to build 246's job-level path.
PART_MAX_ATTEMPTS = 3
# Build 266: free-space floor on the job-data filesystem,
# checked before every part attempt. Below it the attempt fails
# fast with the recorded reason "disk_low" instead of dying
# obscurely mid-encode. A diagnostic guard, not a cleanup
# feature — nothing is ever deleted to satisfy it.
DISK_FLOOR_BYTES = 3 * 1024 * 1024 * 1024
DEFAULT_THRESHOLD_SECONDS = 900
# A part (or the concat) counts as complete when its probed
# duration is within this many seconds of the expected value —
# far above frame/packet slack, far below any real truncation.
DONE_TOLERANCE_S = 1.5
CONCAT_TOLERANCE_S = 1.0


class PartsUnavailable(Exception):
    """Pre-flight could not prepare part mode (segments not
    loadable yet). Raised ONLY before any part work starts;
    run_phase2 catches it and falls through to the classic flow,
    which then decides the job's fate exactly as it would have
    without part mode. Mid-parts failures never raise this —
    they fail the job honestly."""


def _env_file_value(key):
    """The pipeline-side env-settings read (build 259's groq
    pattern): process env first, then the project .env."""
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def threshold_seconds():
    """Admin setting LONG_PARTS_MIN_SECONDS: the source duration
    above which phase 2 runs in part mode. Default 900 (15 min),
    read live like the app's other env settings. Garbage or
    non-positive values fall back to the default; the floor of
    60 s keeps a typo from parting every job."""
    raw = (os.environ.get("LONG_PARTS_MIN_SECONDS") or "").strip() \
        or _env_file_value("LONG_PARTS_MIN_SECONDS")
    try:
        val = int(float(raw))
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD_SECONDS
    if val <= 0:
        return DEFAULT_THRESHOLD_SECONDS
    return max(60, val)


def media_duration(path):
    """The duration the media stages plan on: the container
    duration, clamped to the first video stream's own duration
    when it reports one (the same rule retime.retime applies —
    a container can claim slightly more than its video stream
    holds, and windows must not reach past the picture)."""
    total = probe_duration(path)
    try:
        for s in probe_streams(path):
            if s.get("codec_type") == "video" and s.get("duration"):
                total = min(total, float(s["duration"]))
                break
    except Exception:
        pass
    return total


def part_windows(duration, part_len=PART_SECONDS):
    """Consecutive, complete windows of at most `part_len`
    seconds covering [0, duration]; the last window takes the
    remainder. part_windows(1020) == [(0, 600), (600, 1020)]."""
    duration = float(duration)
    part_len = float(part_len)
    if duration <= 0:
        return []
    wins = []
    t = 0.0
    while t < duration - 0.001:
        wins.append((round(t, 3), round(min(t + part_len, duration), 3)))
        t += part_len
    return wins


def slice_segments(segs, w0, w1):
    """The segments OWNED by window [w0, w1): a segment belongs
    to the part containing its start (w0 <= start < w1); its end
    is clamped to w1. Absolute times are kept (retime trims them
    out of the full source). Sorted by start. The last window's
    w1 is the media duration, so a segment starting exactly at a
    boundary belongs to the part that starts there."""
    out = []
    for seg in segs or []:
        try:
            start = float(seg.get("start") or 0)
            end = float(seg.get("end") or 0)
        except (TypeError, ValueError):
            continue
        if start < w0 or start >= w1 or end <= start:
            continue
        s = dict(seg)
        s["start"] = start
        s["end"] = min(end, w1)
        out.append(s)
    out.sort(key=lambda s: s["start"])
    return out


def rebase_segments(segs, w0):
    """Copies of `segs` shifted so the window start becomes 0
    (the captions part SRT: the burn input-seeks the video, so
    its cues must be part-local)."""
    out = []
    for seg in segs:
        s = dict(seg)
        s["start"] = round(max(0.0, float(seg["start"]) - w0), 3)
        s["end"] = round(max(0.0, float(seg["end"]) - w0), 3)
        out.append(s)
    return out


def slice_fx_pieces(fx, w0, w1):
    """fx for a DUB part: the blur pieces overlapping the window,
    times kept ABSOLUTE — run_phase2/retime remap them through
    the part's retime plan (source time -> part-local retimed
    time) exactly as the classic flow remaps whole-video pieces.
    Everything else in fx (boxes, logo, aspect) is time-free and
    passes through untouched. Returns fx unchanged when there
    are no pieces."""
    if not fx or not fx.get("blur_pieces"):
        return fx
    pieces = []
    for p in fx["blur_pieces"]:
        try:
            t0, t1 = float(p["t0"]), float(p["t1"])
        except (KeyError, TypeError, ValueError):
            continue
        if t1 > w0 and t0 < w1:
            pieces.append(dict(p))
    return {**fx, "blur_pieces": pieces}


def rebase_fx_pieces(fx, w0, w1):
    """fx for a CAPTIONS part: the burn runs on the input-seeked
    (part-local) stream, so the pieces must be part-local too —
    shifted by -w0 and clamped to the window; pieces that end
    up empty are dropped. Everything else passes through."""
    if not fx or not fx.get("blur_pieces"):
        return fx
    pieces = []
    for p in fx["blur_pieces"]:
        try:
            t0 = max(0.0, float(p["t0"]) - w0)
            t1 = min(float(w1) - float(w0), float(p["t1"]) - w0)
        except (KeyError, TypeError, ValueError):
            continue
        if t1 - t0 >= 0.2:
            pieces.append({**p, "t0": round(t0, 2), "t1": round(t1, 2)})
    return {**fx, "blur_pieces": pieces}


_TTS_CARRY = ("tts_file", "tts_text", "tts_duration",
              "tts_provider", "tts_rate", "tts_pitch")


def _inherit_tts(sliced, prev):
    """Carry the previous attempt's tts_* fields onto the fresh
    slice for segments whose (start, end, text) are unchanged,
    so run_phase2's per-part TTS gate can reuse the audio a
    previous attempt already synthesized in this part dir
    instead of re-buying it. Anything edited simply misses the
    match and is re-synthesized, exactly like a fresh render."""
    if not prev:
        return sliced
    by_key = {}
    for seg in prev:
        key = (round(float(seg.get("start") or 0), 3),
               round(float(seg.get("end") or 0), 3),
               (seg.get("text_my") or ""))
        by_key[key] = seg
    for seg in sliced:
        key = (round(float(seg.get("start") or 0), 3),
               round(float(seg.get("end") or 0), 3),
               (seg.get("text_my") or ""))
        old = by_key.get(key)
        if old:
            for f in _TTS_CARRY:
                if f in old:
                    seg[f] = old[f]
    return sliced


def _probe(path):
    try:
        return probe_duration(path)
    except Exception:
        return 0.0


# ---------- build 266: failure notes, accumulated totals, disk floor

FAILURES_NAME = "part_failures.json"
ACCUM_NAME = "accum.json"


class DiskLowError(Exception):
    """The disk floor guard tripped. Recorded with the stable
    error_class "disk_low" (never the class name) so the owner
    sees the reason, not an exception type."""


def _free_bytes(path):
    """Free bytes on the filesystem holding `path` (a separate
    seam so proofs can stub the floor without filling a disk)."""
    return shutil.disk_usage(path).free


def _check_disk(workdir):
    free = _free_bytes(workdir)
    if free < DISK_FLOOR_BYTES:
        raise DiskLowError(
            f"disk space low: {free // (1024 * 1024)} MB free "
            f"(floor {DISK_FLOOR_BYTES // (1024 * 1024)} MB)")


def _write_json_atomic(path, data):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, path)


def load_failures(workdir):
    """The job's recorded part-failure notes ([] when none).
    Read by app.py for the admin job views and by proofs."""
    try:
        notes = json.loads((Path(workdir) / FAILURES_NAME)
                           .read_text(encoding="utf-8"))
        return notes if isinstance(notes, list) else []
    except Exception:
        return []


def _record_failure(workdir, *, part, stage, exc, attempt):
    """Append ONE structured note for a failed part attempt.
    short_message is the exception's text with whitespace
    collapsed, capped at 200 chars — no stack traces, and the
    pipeline's own error strings carry no secrets. A note that
    cannot be written must never mask the real failure, so
    write errors are swallowed."""
    if isinstance(exc, DiskLowError):
        error_class = "disk_low"
    else:
        error_class = type(exc).__name__ or "Error"
    note = {"part": part, "stage": stage,
            "error_class": error_class,
            "short_message": " ".join(str(exc).split())[:200],
            "at": time.time(), "attempt": attempt}
    try:
        notes = load_failures(workdir)
        notes.append(note)
        _write_json_atomic(Path(workdir) / FAILURES_NAME, notes)
    except Exception:
        pass
    return note


def _stage_label(step):
    """Presentation stage for a failure note: the part's own
    step keys fold the way the timings do — tts stays "tts",
    the encode side (retime / finalize / nothing announced
    yet, e.g. the disk guard) reads "render"."""
    return "tts" if step == "tts" else "render"


def _load_accum(workdir, n_parts):
    """Accumulated part-mode totals (build 266): stage seconds,
    per-part wall seconds, concat seconds and per-part attempt
    counters, persisted in parts/accum.json so they survive
    kills and build-246 resumes. A pre-266 job resuming under
    this build has no accum file — seed the stage totals ONCE
    from job.json's phase_s (the last totals the old code
    reported), so the accumulated series continues from what
    the owner already saw instead of restarting below it."""
    base = {"stage_secs": {"tts": 0.0, "retime": 0.0,
                           "finalize": 0.0},
            "part_secs": [0.0] * n_parts,
            "concat_secs": 0.0, "attempts": {}}
    path = Path(workdir) / "parts" / ACCUM_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = None
    if not isinstance(data, dict):
        try:
            ph = json.loads((Path(workdir) / "job.json")
                            .read_text(encoding="utf-8")) \
                .get("phase_s") or {}
            for k in base["stage_secs"]:
                if isinstance(ph.get(k), (int, float)):
                    base["stage_secs"][k] = float(ph[k])
        except Exception:
            pass
        return base
    ss = data.get("stage_secs") or {}
    for k in base["stage_secs"]:
        if isinstance(ss.get(k), (int, float)):
            base["stage_secs"][k] = float(ss[k])
    ps = data.get("part_secs") or []
    for i in range(min(n_parts, len(ps))):
        if isinstance(ps[i], (int, float)):
            base["part_secs"][i] = float(ps[i])
    if isinstance(data.get("concat_secs"), (int, float)):
        base["concat_secs"] = float(data["concat_secs"])
    att = data.get("attempts") or {}
    if isinstance(att, dict):
        base["attempts"] = {str(k): int(v) for k, v in att.items()
                            if isinstance(v, (int, float))}
    return base


def _save_accum(workdir, accum):
    try:
        path = Path(workdir) / "parts" / ACCUM_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(path, accum)
    except Exception:
        pass


def _part_done(workdir, pdir, part_file, mode, w0, w1):
    """A part counts as done ONLY when its job-dir encode exists
    and probes within tolerance of the part's expected duration:
    the window length for captions (the burn preserves timing),
    the part plan's retimed duration for dub/both (retime
    legitimately stretches the timeline). Mirrors build 246's
    rule that an artifact counts only when it is complete."""
    from . import run_job as _rj
    if not _rj._video_ok(part_file):
        return False
    got = _probe(part_file)
    if got <= 0:
        return False
    if mode == "subs":
        return abs(got - (w1 - w0)) <= DONE_TOLERANCE_S
    try:
        plan = json.loads((pdir / "retime_plan.json")
                          .read_text(encoding="utf-8"))
        expected = float(plan["retimed_duration"])
    except Exception:
        return False
    return abs(got - expected) <= DONE_TOLERANCE_S


def _prepare_part_dir(workdir, pdir, src, segs, mode, w0, w1):
    """The part's private workdir: a hardlink to the job's full
    source (no second copy of a possibly-2 GB file; a plain copy
    when the filesystem cannot link), the window's segments
    (absolute times, previous tts_* carried over), and — for
    captions — the part-local rebased SRT the burn reads."""
    pdir.mkdir(parents=True, exist_ok=True)
    link = pdir / "source.mp4"
    if not link.exists():
        try:
            os.link(src, link)
        except OSError:
            shutil.copyfile(src, link)
    sliced = slice_segments(segs, w0, w1)
    prev = None
    seg_file = pdir / "segments_my.json"
    if seg_file.exists():
        try:
            prev = load_segments(seg_file)
        except Exception:
            prev = None
    saved = _inherit_tts(sliced, prev)
    save_segments(saved, seg_file)
    # Build 271: carry the job's word timings into the part
    # (the words owned by this window, absolute times like
    # the part's segments) and build the part's corrected
    # cues from them, so a part's burn and dub placement
    # correct exactly like the whole-video flow's. No words
    # on the job -> the part gets neither file and every
    # line below is build 270's.
    cues = None
    words = cue_timing.load_words(workdir)
    if words:
        wpart = [w for w in words
                 if w0 <= float(w["start"]) < w1]
        if wpart:
            save_segments(wpart, pdir / "words.json")
            cues = cue_timing.build_cues(saved, wpart)
            if cues:
                # A cue never reaches past the window end
                # (slice_segments clamps segment ends the
                # same way); a cue the clamp empties drops.
                cues = [{**c, "end": min(float(c["end"]), w1)}
                        for c in cues
                        if min(float(c["end"]), w1)
                        > float(c["start"])]
                if cues:
                    save_segments(cues, pdir / "cues_my.json")
                else:
                    cues = None
    if mode == "subs":
        if cues:
            write_srt(rebase_segments(cues, w0),
                      pdir / "subs_my.srt")
        else:
            write_srt(rebase_segments(sliced, w0),
                      pdir / "subs_my.srt")
    return sliced


def _concat_parts(workdir, part_files, tmp_final):
    """Concatenate the finished parts into the finalize temp
    file. Stream copy first (the parts are parameter-identical
    by construction); the copy is validated against the sum of
    the parts and any failure or drift falls back to ONE
    re-encode at the finalize family's settings, validated the
    same way. Raises when neither lands a sane file."""
    lst = workdir / "parts" / "concat.txt"
    lst.parent.mkdir(parents=True, exist_ok=True)
    lst.write_text("".join(f"file '{f.resolve()}'\n" for f in part_files),
                   encoding="utf-8")
    expected = sum(_probe(f) for f in part_files)

    def _sane(path):
        if _probe(path) <= 0:
            return False
        if abs(_probe(path) - expected) > CONCAT_TOLERANCE_S:
            return False
        try:
            kinds = {s.get("codec_type") for s in probe_streams(path)}
        except Exception:
            return False
        return "video" in kinds and "audio" in kinds

    try:
        run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
             "-i", str(lst), "-c", "copy", "-f", "mp4", str(tmp_final)],
            timeout=1200)
        if _sane(tmp_final):
            return
    except Exception:
        pass
    # Parameter drift between parts (never expected — the same
    # code path encodes every part): one honest re-encode beats
    # shipping a torn timeline.
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
         "-i", str(lst), "-c:v", "libx264", "-preset", "ultrafast",
         "-crf", str(ENCODE_CRF), "-c:a", "aac", "-b:a", "96k",
         "-f", "mp4", str(tmp_final)], timeout=3600)
    if not _sane(tmp_final):
        raise RuntimeError(
            "အပိုင်းတွေ ပြန်ပေါင်းတာ မရပါ — ခဏနေမှ ပြန်စမ်းပါ "
            "(credit လုံးဝ မဖြတ်ပါ)")


def run_phase2_parts(workdir, *, mode, phase2, progress=None, fx=None,
                     **phase2_kwargs):
    """Phase 2 in part mode. `phase2` is run_job.run_phase2
    (passed in by the caller — this module never imports
    run_job at module level); phase2_kwargs carries the job's
    voice/font/rate/pitch/subs/max_speed/quality/voice_name/
    karaoke settings through to every part unchanged."""
    from . import run_job as _rj

    workdir = Path(workdir)
    src = workdir / "source.mp4"
    final = workdir / "output.mp4"
    if _rj._video_ok(final):
        return final  # a finished output is never redone (246)
    if not _rj._video_ok(src):
        raise PartsUnavailable("no complete source.mp4")
    try:
        segs = load_segments(workdir / "segments_my.json")
    except Exception:
        raise PartsUnavailable("segments_my.json not loadable")
    if not segs:
        raise PartsUnavailable("segments_my.json empty")
    duration = media_duration(src)
    windows = part_windows(duration)
    if len(windows) < 2:
        raise PartsUnavailable("duration does not split")

    # Stage announcements (state only): the aggregate seconds are
    # reported once, at the end, from the per-part captures — the
    # same phase_s wire shape _marks produces, so app.py's fold
    # and the Admin averages see ordinary stage totals.
    def _say(step, state, note="", phase_s=None):
        if not progress:
            return
        try:
            if phase_s is not None:
                progress(step, state, note, phase_s=phase_s)
            else:
                progress(step, state, note)
        except TypeError:
            progress(step, state, note)

    if mode == "subs":
        _say("finalize", "running")
    else:
        _say("tts", "running", phase2_kwargs.get("voice") or "")
        _say("retime", "running")
        _say("finalize", "running")

    # Build 266: totals accumulate across attempts and resumes
    # (parts/accum.json) — the values reported at the end are
    # the accumulated series, never just this run's slice, so a
    # stage total can never drop below a previously reported
    # one. part_secs[i] likewise accumulates per part.
    accum = _load_accum(workdir, len(windows))
    stage_secs = accum["stage_secs"]
    part_secs = accum["part_secs"]
    part_files = []
    for i, (w0, w1) in enumerate(windows):
        pdir = workdir / "parts" / f"part_{i:02d}"
        part_file = workdir / f"part_{i:02d}.mp4"
        part_files.append(part_file)
        if _part_done(workdir, pdir, part_file, mode, w0, w1):
            continue  # completed parts are NEVER re-encoded
        fx_part = slice_fx_pieces(fx, w0, w1) if mode != "subs" \
            else rebase_fx_pieces(fx, w0, w1)
        # Build 266: in-place retries — a failed part is retried
        # here (reusing the part dir and run_phase2's own stage
        # gates) up to PART_MAX_ATTEMPTS total attempts, every
        # failure recorded, before the last error escalates to
        # the job-level path exactly as build 260 raised it.
        last_exc = None
        part_ok = False
        for _try in range(PART_MAX_ATTEMPTS):
            att = int(accum["attempts"].get(str(i), 0)) + 1
            accum["attempts"][str(i)] = att
            _save_accum(workdir, accum)
            captured = {}
            last_step = {"step": None}

            def _part_progress(step, state, note="", phase_s=None,
                               _cap=captured, _ls=last_step):
                _ls["step"] = step
                if phase_s:
                    for k, v in phase_s.items():
                        if isinstance(v, (int, float)):
                            _cap[k] = _cap.get(k, 0.0) + float(v)
                # State (and the job's cancel/shutdown checks,
                # which live in the real progress callback)
                # still flow — the part's own phase_s stays
                # captured, never forwarded, so a half-folded
                # stage total can never pose as the job's.
                _say(step, state, note)

            t0 = time.monotonic()
            try:
                _check_disk(workdir)
                _prepare_part_dir(workdir, pdir, src, segs, mode,
                                  w0, w1)
                # parts_ok=False is load-bearing: the part dir's
                # source is the FULL video (a hardlink), so the
                # phase-2 gate would part the part forever
                # without it.
                out = phase2(pdir, mode=mode,
                             progress=_part_progress, fx=fx_part,
                             parts_ok=False, start_at=w0, end_at=w1,
                             **phase2_kwargs)
                # The part's encode lands in the job dir via
                # temp + rename and must probe as the part it
                # claims to be; anything else fails the attempt
                # honestly with the completed parts left in
                # place.
                tmp_part = workdir / f"part_{i:02d}.tmp.mp4"
                shutil.copyfile(out, tmp_part)
                if not _part_done(workdir, pdir, tmp_part, mode,
                                  w0, w1):
                    try:
                        tmp_part.unlink(missing_ok=True)
                    except Exception:
                        pass
                    raise RuntimeError(
                        f"အပိုင်း {i + 1} ထုတ်တာ မမှန်ပါ — "
                        "ခဏနေမှ ပြန်စမ်းပါ (credit လုံးဝ မဖြတ်ပါ)")
                os.replace(tmp_part, part_file)
            except Exception as e:
                # The attempt's measured stage/wall time is real
                # work — accumulate it before anything else, so
                # the totals stay truthful even when the job
                # level escalates or the process dies next.
                for k in stage_secs:
                    stage_secs[k] += captured.get(k, 0.0)
                part_secs[i] = round(
                    part_secs[i] + (time.monotonic() - t0), 1)
                _save_accum(workdir, accum)
                if type(e).__name__ in ("JobCancelled",
                                         "_ShutdownInterrupt",
                                         "QuotaWallError"):
                    # The owner's own stop signals (raised by the
                    # real progress callback through the part's
                    # stages) — and, build 275, a hard provider
                    # quota/billing wall (pipeline.quota), which
                    # no in-place retry can clear: never retried,
                    # never recorded as a part failure.
                    raise
                _record_failure(workdir, part=i + 1,
                                stage=_stage_label(last_step["step"]),
                                exc=e, attempt=att)
                last_exc = e
                continue
            for k in stage_secs:
                stage_secs[k] += captured.get(k, 0.0)
            part_secs[i] = round(
                part_secs[i] + (time.monotonic() - t0), 1)
            _save_accum(workdir, accum)
            part_ok = True
            break
        if not part_ok:
            raise last_exc

    t0 = time.monotonic()
    tmp_final = workdir / _rj.FINAL_TMP_NAME
    _concat_parts(workdir, part_files, tmp_final)
    os.replace(tmp_final, final)
    accum["concat_secs"] = round(
        accum["concat_secs"] + (time.monotonic() - t0), 1)
    _save_accum(workdir, accum)
    concat_secs = accum["concat_secs"]

    note = {"parts": len(windows),
            "part_secs": [round(s, 1) for s in part_secs]}
    if mode == "subs":
        _say("finalize", "done",
             phase_s={"finalize": round(sum(part_secs) + concat_secs, 1),
                      **note})
    else:
        _say("tts", "done", phase_s={"tts": round(stage_secs["tts"], 1)})
        _say("retime", "done",
             phase_s={"retime": round(stage_secs["retime"], 1)})
        _say("finalize", "done",
             phase_s={"finalize": round(stage_secs["finalize"]
                                        + concat_secs, 1), **note})
    return final
