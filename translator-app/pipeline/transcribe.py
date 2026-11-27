"""Step 2: transcribe speech to timestamped segments.

Engine order (build 259): Groq cloud (whisper-large-v3) FIRST
when a Groq key is configured — ~200x real-time and far more
accurate than the local base model, especially on Chinese.
ANY Groq failure falls back to the local faster-whisper path
(CPU int8; model size from WHISPER_MODEL env, default base)
and the fallback + its error class are recorded in
<workdir>/transcribe_report.json. No key -> local exactly as
before. The Groq path also persists word-level timestamps to
<workdir>/words.json for a later subtitle-timing build.
Fast path: --captions uses a .vtt caption file already fetched by download.py.

CLI: python -m pipeline.transcribe <source.mp4> <workdir> [--captions]
Writes <workdir>/segments_en.json  [{start, end, text}]
"""
import json
import os
import re
import sys
import threading
from pathlib import Path

from . import groq_transcribe
from .common import run, save_segments

MIN_DUR = 0.4  # drop ultra-short noise segments


def _parse_vtt(vtt_path: Path):
    text = Path(vtt_path).read_text(encoding="utf-8", errors="ignore")
    segs, cur = [], None
    ts_re = re.compile(r"(\d+):(\d+):(\d+)[.,](\d+)\s+-->\s+(\d+):(\d+):(\d+)[.,](\d+)")

    def to_sec(m, a, b, c, d):
        return int(m[a]) * 3600 + int(m[b]) * 60 + int(m[c]) + int(m[d]) / 1000.0

    for line in text.splitlines():
        m = ts_re.search(line)
        if m:
            if cur and cur["text"].strip():
                segs.append(cur)
            cur = {"start": to_sec(m, 1, 2, 3, 4), "end": to_sec(m, 5, 6, 7, 8), "text": ""}
        elif cur is not None and line.strip() and not line.startswith(("WEBVTT", "Kind:", "Language:")):
            clean = re.sub(r"<[^>]+>", "", line).strip()
            if clean and clean not in cur["text"]:
                cur["text"] = (cur["text"] + " " + clean).strip()
    if cur and cur["text"].strip():
        segs.append(cur)
    # merge duplicates from rolling auto-captions: keep ones with unique start
    out, seen = [], set()
    for s in segs:
        key = round(s["start"], 1)
        if key in seen or s["end"] - s["start"] < 0.2:
            continue
        seen.add(key)
        out.append(s)
    return out


_MODEL_CACHE = {}
_MODEL_LOCK = threading.Lock()


def _get_model(model_ref):
    """Load the whisper model ONCE per process and reuse it across jobs.

    Loading took seconds (plus a hub metadata round-trip) on every job;
    the app is a long-lived process, so a cached model removes that
    fixed cost entirely. Jobs are NOT serialized — up to four worker
    threads transcribe concurrently (build 180) — so initialization
    is serialized with double-checked locking (build 203, audit
    V07): without it, N simultaneous cold calls each constructed
    their own full ctranslate2 model. Only construction holds the
    lock; inference stays unlocked. The cache is published ONLY
    after a successful construction, so a failed load leaves the
    cache empty and a later call retries."""
    m = _MODEL_CACHE.get(model_ref)
    if m is not None:
        return m
    with _MODEL_LOCK:
        m = _MODEL_CACHE.get(model_ref)
        if m is not None:
            return m
        from faster_whisper import WhisperModel
        # Build 186 (speed audit fix #2): num_workers=2 —
        # ctranslate2's inter_threads follows num_workers, and at
        # the default 1 the ONE shared model instance executed
        # essentially one inference at a time, so concurrent jobs'
        # transcribe() calls queued inside the model (measured:
        # 2 concurrent jobs stretched ~2x each for ~1.3x total
        # throughput). Two workers let two transcriptions truly
        # overlap; model, compute type and every decode setting
        # are unchanged, so a lone job's segments are identical.
        try:
            # Cache-only first: zero network, no hub metadata round-trip.
            m = WhisperModel(model_ref, device="cpu", compute_type="int8",
                             num_workers=2, local_files_only=True)
        except Exception:
            # Fresh server: download once, later loads hit the branch above.
            m = WhisperModel(model_ref, device="cpu", compute_type="int8",
                             num_workers=2)
        _MODEL_CACHE[model_ref] = m
        return m


def _write_report(workdir, report):
    """transcribe_report.json — the transcribe stage's engine
    record (build 259), following translate_report.json's
    convention: which engine produced the transcript, and when
    Groq failed, the fallback + the error class that caused it.
    run_job reads the engine back for the job's timings note.
    Best-effort — a report failure never breaks a job."""
    try:
        (Path(workdir) / "transcribe_report.json").write_text(
            json.dumps(report, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def read_report(workdir):
    """The transcribe_report.json in a workdir ({} when absent
    or unreadable — captions path, pre-259 workdirs)."""
    try:
        return json.loads((Path(workdir) / "transcribe_report.json")
                          .read_text(encoding="utf-8"))
    except Exception:
        return {}


def _transcribe_local(wav, workdir):
    """The pre-259 local faster-whisper path, verbatim: decode
    the extracted 16k mono wav, apply the segment hygiene, save
    segments_en.json, return the segments. Raises RuntimeError
    when no speech is found — exactly as before."""
    # Prefer the project-local model (models/faster-whisper-small) when present
    # so the app is self-contained and never re-downloads; else use the
    # WHISPER_MODEL name (default "small") from the HF hub.
    from .common import PROJECT_ROOT
    local = PROJECT_ROOT / "models" / "faster-whisper-small"
    if (local / "model.bin").exists() and not os.environ.get("WHISPER_MODEL"):
        model_ref = str(local)
    else:
        # Default "base" (user, 2026-11-08: everything fast) — roughly
        # 2x faster decode than "small" at a small accuracy cost;
        # WHISPER_MODEL=small restores the old behaviour.
        model_ref = os.environ.get("WHISPER_MODEL", "base")
    model = _get_model(model_ref)
    # condition_on_previous_text=False: decoding no longer waits on the
    # previous window's text — measurably faster on CPU, and it also
    # stops the repeat-loop hallucinations the conditioning can cause.
    segments, _info = model.transcribe(str(wav), beam_size=1, vad_filter=True,
                                       condition_on_previous_text=False)
    segs = []
    for s in segments:
        t = (s.text or "").strip()
        if not t or (s.end - s.start) < 0.2:
            continue
        segs.append({"start": round(float(s.start), 3),
                     "end": round(float(s.end), 3), "text": t})
    if not segs:
        raise RuntimeError("no speech segments found by whisper")
    save_segments(segs, workdir / "segments_en.json")
    return segs


def transcribe(video_path, workdir, use_captions=False):
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    if use_captions:
        vtts = sorted(workdir.glob("source.*.vtt"))
        if vtts:
            segs = _parse_vtt(vtts[0])
            if segs:
                save_segments(segs, workdir / "segments_en.json")
                return segs
        # fall through to whisper if captions unusable

    # extract 16k mono wav (the local path decodes it; the Groq
    # path cuts its upload chunks from it)
    wav = workdir / "audio16k.wav"
    run(["ffmpeg", "-y", "-v", "error", "-i", str(video_path),
         "-vn", "-ac", "1", "-ar", "16000", str(wav)])

    # Build 259: Groq FIRST when a key is configured. ANY Groq
    # failure (network, HTTP error, quota, malformed or empty
    # result) falls back to the local path, and the fallback is
    # recorded with the Groq error class. No key -> local only,
    # byte-for-byte the pre-259 behaviour.
    groq_error = None
    if groq_transcribe.key_configured():
        try:
            got = groq_transcribe.transcribe_audio(wav, workdir)
        except Exception as e:  # GroqError or anything unexpected
            groq_error = {"kind": getattr(e, "kind", "error"),
                          "detail": str(e)[:200],
                          "route": getattr(e, "route", "")}
        else:
            segs = got["segments"]
            save_segments(segs, workdir / "segments_en.json")
            if got.get("words"):
                # Word-level timestamps for a later
                # subtitle-timing build; no consumer reads
                # this yet, so segments_en.json keeps its
                # exact pre-259 shape.
                save_segments(got["words"], workdir / "words.json")
            _write_report(workdir, {
                "engine": "groq", "model": got["model"],
                "chunks": got["chunks"],
                "language": got.get("language") or "",
                "duration": got.get("duration") or 0,
                "route": got.get("route") or ""})
            return segs

    segs = _transcribe_local(wav, workdir)
    if groq_error is not None:
        _write_report(workdir, {
            "engine": "local-fallback",
            "groq_error": groq_error["kind"],
            "groq_error_detail": groq_error["detail"],
            "route": groq_error["route"]})
    else:
        _write_report(workdir, {"engine": "local"})
    return segs


if __name__ == "__main__":
    use_cap = "--captions" in sys.argv
    res = transcribe(sys.argv[1], Path(sys.argv[2]), use_captions=use_cap)
    print(f"{len(res)} segments")
