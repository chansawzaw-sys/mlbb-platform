"""Shared ffmpeg/ffprobe helpers."""
import contextlib
import contextvars
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

_HAS_NICE = shutil.which("nice") is not None

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FONTS_DIR = PROJECT_ROOT / "assets" / "fonts"


def ensure_fonts():
    """Materialize the bundled Myanmar fonts. The fonts are BINARY
    and the deploy channel (git via a text-only push) cannot carry
    .ttf files, so the repo ships them as .ttf.b64 text; decode any
    that are missing next to their encoded copy. Without this the
    server had NO Myanmar font at all and libass drew every Burmese
    glyph as an empty box (owner reports 2026-11-09)."""
    import base64
    try:
        if not FONTS_DIR.exists():
            return
        for b64 in FONTS_DIR.glob("*.ttf.b64"):
            ttf = b64.with_suffix("")
            if not ttf.exists() or ttf.stat().st_size < 10_000:
                ttf.write_bytes(base64.b64decode(
                    b64.read_text(encoding="ascii")))
    except Exception:
        pass

_CA_PATCHED = False


def ensure_system_ca():
    """Point certifi at the OS CA bundle when one exists.

    Some networks (corporate / sandbox MITM proxies) use a CA that is in the
    OS store (/etc/ssl/certs/ca-certificates.crt) but NOT in certifi's bundle.
    yt-dlp, aiohttp (edge-tts) and requests all consult certifi, so without
    this they fail with CERTIFICATE_VERIFY_FAILED while curl works fine.
    Safe no-op where the OS bundle does not exist.
    """
    global _CA_PATCHED
    if _CA_PATCHED:
        return
    _CA_PATCHED = True
    sys_bundle = Path("/etc/ssl/certs/ca-certificates.crt")
    if sys_bundle.exists():
        try:
            import certifi
            certifi.where = lambda: str(sys_bundle)  # noqa: E731
        except Exception:
            pass


# --- Adaptive encode threads (build 198) -----------------------------------
# Build 193 capped every libx264 encode at a literal "-threads 2"
# so up to four concurrent jobs on the 4-core box could not starve
# the web server sharing it. The cap was unconditional, though: a
# job running ALONE also got only 2 of the 4 cores. The job manager
# (app.py) registers its active-worker count here at startup; an
# encode built while this process's job is the only active one may
# use all 4 threads, and the moment 2+ jobs are active the build-193
# cap of 2 applies again — so the no-oversubscription property is
# unchanged exactly when it matters. Standalone CLI use (no
# provider registered) is a single job by definition.
_ACTIVE_JOBS_PROVIDER = None


def set_active_jobs_provider(fn):
    """Register the job manager's active-job counter (app.py calls
    this once at startup with its _active_worker_count)."""
    global _ACTIVE_JOBS_PROVIDER
    _ACTIVE_JOBS_PROVIDER = fn


def active_job_count():
    """Jobs actually RUNNING right now (queued jobs do not count —
    the provider's contract, mirroring app.py's worker_started
    rule). 1 when no provider is registered (standalone CLI)."""
    fn = _ACTIVE_JOBS_PROVIDER
    if fn is not None:
        try:
            return max(0, int(fn()))
        except Exception:
            pass
    return 1


def encode_threads():
    """libx264 -threads for an encode built at this moment: 4 when
    this is the only active job, else the build-193 cap of 2."""
    return 4 if active_job_count() <= 1 else 2


def capped_dims(width, height, cap_h):
    """Build 267: (w, h) scaled down so the SHORTER side is at
    most `cap_h`, aspect preserved, both dims even (yuv420p),
    NEVER upscaled — or None when the frame is already within
    the cap (or the dims are unusable). The shorter side is the
    "p" dimension on both orientations: 1920x1080 -> 1280x720,
    1080x1920 -> 720x1280, 1280x720 and 720x1280 pass through
    unchanged."""
    try:
        w, h = int(width), int(height)
        cap_h = int(cap_h)
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0 or cap_h <= 0:
        return None
    short = min(w, h)
    if short <= cap_h:
        return None
    f = float(cap_h) / short
    nw = max(2, int(round(w * f / 2.0)) * 2)
    nh = max(2, int(round(h * f / 2.0)) * 2)
    if (nw, nh) == (w, h):
        return None
    return (nw, nh)


def maxrate_cap_k(quality):
    """Encode maxrate ceiling (kbps) for a job's download quality
    (build 167). "hd" jobs fetched the original rendition on the
    owner's explicit order — the build-102 ceiling of 2500k would
    strangle it straight back to blurry, so hd gets 6000k. The
    source anchor in target_maxrate_k still keeps a low-bitrate
    source from ballooning; the cap only stops being the limit.
    Build 279 raised both ceilings (16000 hd / 12000 else): the
    ceiling must never strangle the CRF — the actual value comes
    from target_maxrate_k, anchored to the source's own bitrate
    with a per-resolution floor on top."""
    return 16000 if quality == "hd" else 12000


def _res_floor_k(width, height):
    """Build 279: the maxrate floor (kbps) a frame of this size
    needs so the encode can match source quality — 1080p-class
    frames 10000, 720p-class 6000, smaller frames no floor (the
    source anchor rules). "Class" is the SHORTER side — the
    same "p" measure capped_dims uses — so a portrait frame
    counts by its width."""
    short = min(width, height) if width and height else 0
    if short >= 1000:
        return 10000
    if short >= 700:
        return 6000
    return 0


def target_maxrate_k(input_mp4, cap_k=2500):
    """Video maxrate (kbps) that keeps a re-encode in the SOURCE's size
    class. CRF alone is quality-based: re-encoding a low-bitrate phone
    or TikTok source at crf 23/24 ballooned one 12 MB job to 110 MB.
    Anchor on the original source.mp4 sitting next to the input when
    there is one (dub jobs burn the retimed intermediate, whose own
    bitrate is already one re-encode away from the source).
    Build 279: the value is max(anchor x 1.1, the resolution floor
    for the encode input's own frame size — _res_floor_k), bounded
    above by cap_k except that the floor itself wins even over a
    stale low cap, so no forgotten 2500 default can strangle a
    1080p frame. The maxrate ceiling is also what keeps the CRF
    honest on size: a low-bitrate source cannot balloon past
    roughly its floor class."""
    try:
        src = Path(input_mp4)
        floor = 0
        for s in probe_streams(src):
            if s.get("codec_type") == "video":
                floor = _res_floor_k(int(s.get("width") or 0),
                                     int(s.get("height") or 0))
                break
        anchor = src.with_name("source.mp4")
        if anchor.exists():
            src = anchor
        dur = probe_duration(src)
        if dur <= 0:
            return max(1800, floor)
        kbps = src.stat().st_size * 8 / dur / 1000.0
        val = max(kbps * 1.1, float(floor))
        return int(max(450, min(max(cap_k, floor), val)))
    except Exception:
        return 1800


# Build 279: the ONE video-encode recipe for every render path
# (burn finalize, fx, retime intermediates AND finals, the parts
# concat fallback): libx264 ultrafast — the builds 265-267 speed
# programme's preset; a slower preset would multiply render times
# for a gain the CRF already delivers — at CRF 19, inside the
# 18-20 visually-lossless band, with the maxrate from
# target_maxrate_k so the CRF is never strangled by a bitrate
# ceiling sized for a smaller frame. Build 278's CRF 23/24 under
# the old 6000k/2500k caps was the visible quality loss the owner
# reported on 2026-11-28. The retime intermediates use the same
# recipe because the single-pass dub (no fx) ships retimed.mp4
# itself as the final output.
ENCODE_CRF = 19


def x264_args(input_mp4, cap_k=2500):
    """The shared libx264 argument block for one encode of
    `input_mp4` (build 279; see ENCODE_CRF above)."""
    mr = target_maxrate_k(input_mp4, cap_k)
    return ["-c:v", "libx264", "-preset", "ultrafast",
            "-crf", str(ENCODE_CRF),
            "-maxrate", f"{mr}k", "-bufsize", f"{2 * mr}k"]


# --- Job-scoped process tracking (build 204, audit V09) ------------------
# Cancellation used to be a flag the worker noticed at the next step
# boundary: a running ffmpeg kept encoding to completion (or to its
# own — sometimes absent — timeout) after the user cancelled. Now
# run() registers every child it spawns under the job whose worker
# thread is executing it, and app.py's cancel path terminates those
# process GROUPS on request. The job id travels in a ContextVar set
# by the job worker (app.py _worker/_worker2) for the whole run:
# every pipeline phase executes synchronously on that thread (the
# TTS fallback pool is the one exception — its per-segment calls
# are short network/convert steps, and the long encodes all happen
# on the worker thread). Anything spawned outside a job context —
# startup probes, request-thread thumbnails, CLI use — is never
# registered and behaves exactly as before.
_JOB_CONTEXT = contextvars.ContextVar("vt_job_id", default=None)


@contextlib.contextmanager
def job_context(job_id):
    """Mark this thread's subprocesses as belonging to `job_id`
    for the duration of the block (app.py's job workers)."""
    token = _JOB_CONTEXT.set(job_id)
    try:
        yield
    finally:
        _JOB_CONTEXT.reset(token)


def current_job_id():
    return _JOB_CONTEXT.get()


_JOB_PROCS = {}  # job_id -> set of live Popen objects
_JOB_PROCS_LOCK = threading.Lock()


def _register_proc(job_id, proc):
    with _JOB_PROCS_LOCK:
        _JOB_PROCS.setdefault(job_id, set()).add(proc)


def _unregister_proc(job_id, proc):
    with _JOB_PROCS_LOCK:
        procs = _JOB_PROCS.get(job_id)
        if procs is not None:
            procs.discard(proc)
            if not procs:
                _JOB_PROCS.pop(job_id, None)


def job_processes(job_id):
    """Snapshot of the job's currently registered processes."""
    with _JOB_PROCS_LOCK:
        return list(_JOB_PROCS.get(job_id) or ())


def _signal_group(proc, sig):
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def terminate_job_processes(job_id, grace=5.0):
    """Stop every process registered under `job_id`: SIGTERM to
    each process GROUP (run() spawns with start_new_session=True,
    so each child leads its own group and its children die with
    it), up to `grace` seconds to exit, then SIGKILL. Returns how
    many processes were signalled. Other jobs' processes are never
    touched — the registry is keyed by job id."""
    procs = job_processes(job_id)
    live = [p for p in procs if p.poll() is None]
    for p in live:
        _signal_group(p, signal.SIGTERM)
    deadline = time.monotonic() + max(0.0, grace)
    for p in live:
        remain = deadline - time.monotonic()
        if remain <= 0:
            break
        try:
            p.wait(timeout=remain)
        except Exception:
            pass
    stubborn = [p for p in live if p.poll() is None]
    for p in stubborn:
        _signal_group(p, signal.SIGKILL)
    for p in stubborn:
        try:
            p.wait(timeout=5)
        except Exception:
            pass
    return len(live)


# Default deadline for run() callers that pass no timeout of their
# own (build 204, audit V09). retime's explicit ceilings top out at
# 1800 s; 3600 s leaves generous headroom over the longest
# legitimate encode while guaranteeing no child can run forever.
DEFAULT_RUN_TIMEOUT = 3600


def run(cmd, **kw):
    """Run a command, raise RuntimeError with stderr tail on failure.

    ffmpeg runs under `nice` so a long encode can never starve the
    rest of this small shared server (the MLBB app lives on it too);
    the encode still gets every idle cycle, it just yields politely.

    Build 204 (audit V09): the child is spawned in its own session
    (its own process group) and, when a job context is active,
    registered under that job so cancellation can terminate the
    whole group at once. A caller that passes no `timeout` gets
    DEFAULT_RUN_TIMEOUT; a timeout raises subprocess.TimeoutExpired
    exactly as subprocess.run did (retime's canary fallback depends
    on that), after the timed-out group has been killed."""
    if cmd and cmd[0] == "ffmpeg" and _HAS_NICE:
        cmd = ["nice", "-n", "8"] + [str(c) for c in cmd]
    timeout = kw.pop("timeout", None)
    if timeout is None:
        timeout = DEFAULT_RUN_TIMEOUT
    job_id = _JOB_CONTEXT.get()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True,
                         start_new_session=True, **kw)
    if job_id:
        _register_proc(job_id, p)
    try:
        stdout, stderr = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _signal_group(p, signal.SIGKILL)
        try:
            p.communicate(timeout=10)
        except Exception:
            pass
        raise
    except BaseException:
        # The waiting thread itself was interrupted: do not leave
        # the child running unsupervised behind us.
        if p.poll() is None:
            _signal_group(p, signal.SIGKILL)
        raise
    finally:
        if job_id:
            _unregister_proc(job_id, p)
    done = subprocess.CompletedProcess(cmd, p.returncode, stdout, stderr)
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "")[-2000:]
        raise RuntimeError(f"command failed ({' '.join(str(c) for c in cmd[:4])}...):\n{tail}")
    return done


def probe_duration(path):
    p = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(path)])
    return float(json.loads(p.stdout)["format"]["duration"])


def probe_streams(path):
    p = run(["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,width,height,r_frame_rate,duration,channels,channel_layout",
             "-of", "json", str(path)])
    return json.loads(p.stdout)["streams"]


def video_fps(path):
    """Probed frame rate (float) of the first video stream in
    `path`, or None when it cannot be determined (build 201).
    r_frame_rate arrives as a fraction string ("30000/1001"); a
    zero denominator, a zero/garbled value or any probe failure
    means 'unknown' — callers must treat None as 'keep the
    previous behaviour exactly'."""
    try:
        for s in probe_streams(path):
            if s.get("codec_type") == "video":
                raw = str(s.get("r_frame_rate") or "").strip()
                if "/" in raw:
                    num, den = raw.split("/", 1)
                    den = float(den)
                    if not den:
                        return None
                    return float(num) / den
                val = float(raw)
                return val if val > 0 else None
    except Exception:
        return None
    return None


def fmt_ts(seconds):
    """SRT timestamp."""
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms == 1000:
        s += 1
        ms = 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(segments, out_path, text_key="text_my", start_key="start", end_key="end"):
    lines = []
    for i, seg in enumerate(segments, 1):
        text = (seg.get(text_key) or "").strip()
        if not text:
            continue
        lines.append(str(i))
        lines.append(f"{fmt_ts(seg[start_key])} --> {fmt_ts(seg[end_key])}")
        lines.append(text)
        lines.append("")
    Path(out_path).write_text("\n".join(lines), encoding="utf-8")
    return out_path


def load_segments(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_segments(segments, path):
    # Build 246: atomic write (temp file in the same directory +
    # os.replace) so a kill mid-write can never leave a torn segments
    # file behind — the stage-resume gates in run_job trust these
    # files as stage-completion evidence.
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(segments, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)
