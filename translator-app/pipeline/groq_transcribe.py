"""Groq cloud transcription — build 259, the PRIMARY speech-to-text
engine whenever the owner has configured a Groq API key.

Why: the local path (pipeline/transcribe.py) runs faster-whisper's
base model on this small VPS's CPU — minutes for a long video, and
the base model mis-hears Chinese, which then poisons the
translation downstream. Groq serves whisper-large-v3 at roughly
200x real-time with far better accuracy, over a plain
OpenAI-compatible endpoint. Groq is NOT geo-blocked from this
server, so the calls go out DIRECT HTTPS — no VPN tunnel (contrast
OpenAI/Gemini, which must ride tunnel.py).

Key handling (security): read ONLY from env GROQ_API_KEY or the
git-ignored `.env` in the project root (same pattern as
openai_translate); saved via the app's settings endpoint behind
the admin login; never returned, never logged.

Size handling: the free tier caps an upload at 25 MB. Audio
longer than CHUNK_SECONDS is cut into consecutive chunks,
encoded per chunk to 48 kbps mono mp3 (a 10-minute chunk is
~3.6 MB — far under the cap) and transcribed sequentially.
Chunk i OWNS the window [i*CHUNK_SECONDS, (i+1)*CHUNK_SECONDS)
but is SENT CHUNK_OVERLAP_S of extra audio past its nominal end,
so a segment straddling the boundary is heard whole by the chunk
that owns it; the merge keeps, from each chunk, only the
segments/words whose start falls inside its owned window (the
last chunk keeps everything). No duplicates, no coverage gaps.

Output: {"segments": [{start, end, text}], "words":
[{start, end, word}], "language": str, "duration": float,
"model": str, "chunks": int} — segment shape identical to the
local path's (seconds rounded to 3 decimals, empty/ultra-short
dropped). ANY failure raises GroqError with a machine-readable
.kind; the caller (transcribe.py) falls back to local whisper
and records the kind.
"""
import json
import os
import time
from pathlib import Path

from .common import PROJECT_ROOT, probe_duration, run

_API_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
MODELS = ("whisper-large-v3", "whisper-large-v3-turbo")
DEFAULT_MODEL = "whisper-large-v3"
FREE_TIER_MAX_BYTES = 25 * 1024 * 1024
CHUNK_SECONDS = 600.0      # ~10 min of audio per request
CHUNK_OVERLAP_S = 3.0      # extra audio sent past each nominal end
_MIN_SEG_DUR = 0.2         # same hygiene floor as the local path
_STATUS_NAME = ".groq_last_status"


class GroqError(RuntimeError):
    """A Groq failure with a machine-readable `kind`:
    http_429 / http_401 / http_403 / http_5xx / http_other /
    network / bad_response / empty / no_key."""

    def __init__(self, kind, detail=""):
        super().__init__(f"groq {kind}: {detail}"[:300])
        self.kind = kind
        self.detail = str(detail)[:200]


def _env_file_value(key):
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def get_api_key():
    key = (os.environ.get("GROQ_API_KEY") or "").strip()
    if key:
        return key
    return _env_file_value("GROQ_API_KEY")


def key_configured():
    return bool(get_api_key())


def get_model():
    model = (os.environ.get("GROQ_MODEL") or "").strip() \
        or _env_file_value("GROQ_MODEL")
    return model if model in MODELS else DEFAULT_MODEL


def _note_status(ok, detail):
    """Last-outcome breadcrumb for the Admin card (mirrors
    openai_translate._note_error, but successes are noted too —
    the card shows the last Groq success OR failure)."""
    try:
        (PROJECT_ROOT / _STATUS_NAME).write_text(
            f"{int(time.time())} {'ok' if ok else 'error'} "
            f"{detail}"[:300], encoding="utf-8")
    except Exception:
        pass


def last_status():
    """{"ts": epoch, "ok": bool, "detail": str} of the most
    recent Groq attempt, or None when there has never been one."""
    try:
        raw = (PROJECT_ROOT / _STATUS_NAME).read_text(
            encoding="utf-8").split(" ", 2)
        return {"ts": int(raw[0]), "ok": raw[1] == "ok",
                "detail": raw[2] if len(raw) > 2 else ""}
    except Exception:
        return None


def _post_transcription(chunk_path, model, language, timeout):
    """THE HTTP seam — one multipart POST to Groq, returning the
    parsed verbose_json payload. Raises GroqError on any HTTP or
    transport problem. (proof259 stubs this function; everything
    above and below it runs for real.)"""
    import requests

    key = get_api_key()
    if not key:
        raise GroqError("no_key")
    chunk_path = Path(chunk_path)
    mime = "audio/mpeg" if chunk_path.suffix == ".mp3" else "audio/wav"
    data = [("model", model),
            ("response_format", "verbose_json"),
            ("timestamp_granularities[]", "segment"),
            ("timestamp_granularities[]", "word")]
    if language:
        data.append(("language", language))
    try:
        with open(chunk_path, "rb") as fh:
            resp = requests.post(
                _API_URL,
                headers={"Authorization": f"Bearer {key}"},
                files={"file": (chunk_path.name, fh, mime)},
                data=data, timeout=timeout)
    except Exception as e:  # DNS / connect / read timeout / TLS
        raise GroqError("network", e) from e
    if resp.status_code == 429:
        raise GroqError("http_429", resp.text[:150])
    if resp.status_code in (401, 403):
        raise GroqError(f"http_{resp.status_code}", resp.text[:150])
    if resp.status_code >= 500:
        raise GroqError("http_5xx",
                        f"{resp.status_code} {resp.text[:120]}")
    if resp.status_code != 200:
        raise GroqError("http_other",
                        f"{resp.status_code} {resp.text[:120]}")
    try:
        return resp.json()
    except Exception as e:
        raise GroqError("bad_response", f"unparseable JSON: {e}") from e


def _windows(duration):
    """Consecutive ownership windows [(start, nominal_end)];
    nominal_end is None for a single window of unknown length
    (duration probe failed — the whole file is one chunk)."""
    if not duration or duration <= CHUNK_SECONDS:
        return [(0.0, duration or None)]
    wins = []
    t = 0.0
    while t < duration:
        wins.append((t, min(duration, t + CHUNK_SECONDS)))
        t += CHUNK_SECONDS
    return wins


def _make_chunk(wav, out, start, send_end):
    """Cut [start, send_end] out of the 16k mono wav and encode
    it as a 48 kbps mono mp3 (small enough that the 25 MB cap
    can never bite at these lengths). send_end None = to EOF."""
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-ss", f"{start:.3f}", "-i", str(wav)]
    if send_end is not None:
        cmd += ["-t", f"{send_end - start:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", "16000",
            "-codec:a", "libmp3lame", "-b:a", "48k", str(out)]
    run(cmd)


def _clean(items, offset, keep_before, text_key, min_dur):
    """Offset one chunk's segments/words to absolute time and
    apply the merge + hygiene rules: keep only items starting
    inside the chunk's owned window (keep_before None = keep
    all, i.e. the last chunk), drop empty text and items
    shorter than min_dur (the local path's segment floor for
    segments; ~zero for words — a spoken word is often shorter
    than a segment floor), round to 3 decimals."""
    out = []
    for it in items or []:
        try:
            start = offset + float(it.get("start"))
            end = offset + float(it.get("end"))
        except (TypeError, ValueError):
            continue
        text = (it.get(text_key) or "").strip()
        if not text or end - start < min_dur:
            continue
        if keep_before is not None and start >= keep_before:
            continue  # owned by the next chunk
        out.append({"start": round(start, 3), "end": round(end, 3),
                    text_key: text})
    return out


def transcribe_audio(wav_path, workdir, language=None):
    """Transcribe a wav file via Groq. Returns the contract
    dict from the module docstring. Raises GroqError on ANY
    failure — the caller falls back to local whisper."""
    try:
        return _transcribe_audio(wav_path, Path(workdir), language)
    except GroqError as e:
        _note_status(False, f"{e.kind} {e.detail}"[:200])
        raise


def _transcribe_audio(wav_path, workdir, language):
    wav = Path(wav_path)
    model = get_model()
    try:
        duration = probe_duration(wav)
    except Exception:
        duration = 0.0
    wins = _windows(duration)
    single = len(wins) == 1
    chunk_dir = workdir / "groq_chunks"
    segments, words = [], []
    language_out, duration_out = "", float(duration or 0.0)
    for i, (start, nominal_end) in enumerate(wins):
        last = i == len(wins) - 1
        if single and wav.stat().st_size <= FREE_TIER_MAX_BYTES:
            chunk_path = wav  # short enough: upload as-is
            send_dur = duration
        else:
            send_end = nominal_end
            if not last and nominal_end is not None:
                send_end = nominal_end + CHUNK_OVERLAP_S
                if duration:
                    send_end = min(send_end, duration)
            chunk_dir.mkdir(parents=True, exist_ok=True)
            chunk_path = chunk_dir / f"chunk_{i:03d}.mp3"
            _make_chunk(wav, chunk_path, start, send_end)
            send_dur = (send_end - start) if send_end is not None \
                else (duration - start if duration else 0.0)
        timeout = int(min(1800, max(240, 240 + 2 * (send_dur or 0))))
        payload = _post_transcription(chunk_path, model, language,
                                      timeout)
        if not isinstance(payload, dict) \
                or not isinstance(payload.get("segments"), list):
            raise GroqError("bad_response", "no segments list")
        keep_before = None if last else nominal_end
        for s in _clean(payload.get("segments"), start, keep_before,
                        "text", _MIN_SEG_DUR):
            segments.append({"start": s["start"], "end": s["end"],
                             "text": s["text"]})
        words.extend(_clean(payload.get("words"), start, keep_before,
                            "word", 0.0))
        if payload.get("language"):
            language_out = str(payload["language"])
        if payload.get("duration"):
            try:
                duration_out = max(duration_out,
                                   start + float(payload["duration"]))
            except (TypeError, ValueError):
                pass
    segments.sort(key=lambda s: s["start"])
    words.sort(key=lambda w: w["start"])
    if not segments:
        raise GroqError("empty", "groq returned no usable segments")
    _note_status(True, model)
    return {"segments": segments, "words": words,
            "language": language_out, "duration": round(duration_out, 3),
            "model": model, "chunks": len(wins)}


if __name__ == "__main__":
    print("groq key configured:", key_configured(),
          "| model:", get_model(), "| last:", last_status())
