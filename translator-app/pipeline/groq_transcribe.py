"""Groq cloud transcription — build 259, the PRIMARY speech-to-text
engine whenever the owner has configured a Groq API key.

Why: the local path (pipeline/transcribe.py) runs faster-whisper's
base model on this small VPS's CPU — minutes for a long video, and
the base model mis-hears Chinese, which then poisons the
translation downstream. Groq serves whisper-large-v3 at roughly
200x real-time with far better accuracy, over a plain
OpenAI-compatible endpoint.

Routing (build 263): builds 259–262 sent the calls DIRECT,
believing Groq was not geo-blocked from this server. Live
evidence 2026-11-26 20:02 disproved that: a direct attempt was
refused with http_403 {"error":{"message":"Forbidden"}} — an
IP/region-class block on this Hong Kong datacenter route (a bad
key would be a 401), and the job silently fell back to local
whisper. The calls now ride the owner's Singapore tunnel FIRST
— the same per-call `proxies` from tunnel.requests_proxies()
that the Gemini/OpenAI clients use, never a global proxy. If
the failure is at the proxy layer itself (proxy unreachable /
refused — Groq never saw the request), exactly ONE direct
attempt follows; if Groq itself answered on the tunnel route
(any HTTP status, 403 included), there is NO direct retry —
the direct route is already proven blocked from this host, and
re-uploading megabytes of audio would be waste. Total attempts
per chunk are capped at 2. Tunnel down (not verified) -> the
direct route only, exactly the pre-263 behaviour. Every
attempt's route + outcome is recorded in the .groq_last_status
breadcrumb and in the result/report "route" field.

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
"model": str, "chunks": int, "route": str} — segment shape
identical to the local path's (seconds rounded to 3 decimals,
empty/ultra-short dropped); "route" (build 263) names the
route(s) that carried the successful chunks ("tunnel",
"direct", or "tunnel+direct" if the tunnel died mid-job).
ANY failure raises GroqError with a machine-readable
.kind (and .route); the caller (transcribe.py) falls back
to local whisper and records the kind.
"""
import json
import os
import threading
import time
from pathlib import Path

from . import tunnel
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
    network / bad_response / empty / no_key. Build 263 adds
    `route`: the route of the decisive attempt ("tunnel" /
    "direct", "" when no attempt was made, e.g. no_key)."""

    def __init__(self, kind, detail=""):
        super().__init__(f"groq {kind}: {detail}"[:300])
        self.kind = kind
        self.detail = str(detail)[:200]
        self.route = ""


class _ProxyLayerError(Exception):
    """A proxied attempt died AT the proxy layer (connection
    refused / proxy error / connect timeout to the local
    127.0.0.1:18119 proxy): Groq never saw the request, so the
    one direct retry in _post_transcription is safe. Any other
    transport failure is a plain GroqError("network") — the
    request may have reached Groq, so no retry."""


# Per-thread log of the current transcribe_audio call's chunk
# attempts ("tunnel:ok", "tunnel:proxy", "direct:http_403", …)
# — jobs run in parallel threads, and each job's breadcrumb /
# report must describe ITS OWN attempts only.
_ATTEMPTS = threading.local()


def _attempts():
    lst = getattr(_ATTEMPTS, "list", None)
    if lst is None:
        lst = _ATTEMPTS.list = []
    return lst


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


def _note_status(ok, detail, route=""):
    """Last-outcome breadcrumb for the Admin card (mirrors
    openai_translate._note_error, but successes are noted too —
    the card shows the last Groq success OR failure). Build
    263: a "route=<route>" token rides between the status word
    and the detail when a route is known; records written
    before 263 have no token and parse with route "" — the
    detail text itself is unchanged either way."""
    try:
        rec = f"{int(time.time())} {'ok' if ok else 'error'} "
        if route:
            rec += f"route={route} "
        rec += str(detail)
        (PROJECT_ROOT / _STATUS_NAME).write_text(
            rec[:300], encoding="utf-8")
    except Exception:
        pass


def last_status():
    """{"ts": epoch, "ok": bool, "detail": str, "route": str}
    of the most recent Groq attempt, or None when there has
    never been one. `route` (build 263) is "" for records
    written before the field existed."""
    try:
        raw = (PROJECT_ROOT / _STATUS_NAME).read_text(
            encoding="utf-8").split(" ", 2)
        rest = raw[2] if len(raw) > 2 else ""
        route = ""
        if rest.startswith("route="):
            head, _, tail = rest.partition(" ")
            route = head[len("route="):]
            rest = tail
        return {"ts": int(raw[0]), "ok": raw[1] == "ok",
                "detail": rest, "route": route}
    except Exception:
        return None


def _post_once(chunk_path, model, language, timeout, proxies):
    """ONE multipart POST attempt (build 263). `proxies` is the
    tunnel dict from tunnel.requests_proxies(), or None for a
    direct attempt — in which case the `proxies` kwarg is
    omitted entirely, byte-for-byte the pre-263 call. The
    chunk file is opened per attempt, so a retry re-reads it
    from byte 0 and the multipart body goes out intact on
    whichever route carries the attempt. Returns the parsed
    verbose_json payload; raises GroqError on any HTTP error
    or non-proxy transport problem, and _ProxyLayerError when
    a PROXIED attempt died at the proxy layer (the local proxy
    refused / errored / the connect timed out — with a proxy
    in play the connect phase targets 127.0.0.1:18119, so no
    HTTP request ever reached Groq)."""
    import requests

    key = get_api_key()
    chunk_path = Path(chunk_path)
    mime = "audio/mpeg" if chunk_path.suffix == ".mp3" else "audio/wav"
    data = [("model", model),
            ("response_format", "verbose_json"),
            ("timestamp_granularities[]", "segment"),
            ("timestamp_granularities[]", "word")]
    if language:
        data.append(("language", language))
    kwargs = {}
    if proxies:
        kwargs["proxies"] = proxies
    try:
        with open(chunk_path, "rb") as fh:
            resp = requests.post(
                _API_URL,
                headers={"Authorization": f"Bearer {key}"},
                files={"file": (chunk_path.name, fh, mime)},
                data=data, timeout=timeout, **kwargs)
    except (requests.exceptions.ProxyError,
            requests.exceptions.ConnectTimeout) as e:
        if proxies:
            raise _ProxyLayerError(str(e)[:150]) from e
        raise GroqError("network", e) from e
    except Exception as e:  # DNS / read timeout / TLS / reset
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


def _post_transcription(chunk_path, model, language, timeout):
    """THE HTTP seam — the routed transcription POST, returning
    the parsed verbose_json payload. Raises GroqError on any
    failure. (proof259 stubs this function; everything above
    and below it runs for real.)

    Build 263 routing (see the module docstring): the tunnel
    route goes FIRST while the tunnel is verified up — via
    tunnel.requests_proxies(), the same helper
    tunnel.request_kwargs() wraps for the Gemini/OpenAI
    clients (request_kwargs itself cannot express the forced
    direct retry below, so the routing reads the helper
    directly). Exactly ONE direct attempt follows a
    proxy-layer failure; a Groq answer on the tunnel route —
    any HTTP status — is final for this chunk, as is a
    non-proxy transport failure there. Tunnel not verified ->
    the direct route only. Every attempt is logged to the
    per-thread attempt list for the breadcrumb/report."""
    if not get_api_key():
        raise GroqError("no_key")
    attempts = _attempts()
    proxies = tunnel.requests_proxies()
    if proxies:
        try:
            payload = _post_once(chunk_path, model, language,
                                 timeout, proxies)
        except _ProxyLayerError:
            attempts.append("tunnel:proxy")
            # fall through to the single direct attempt
        except GroqError as e:
            attempts.append(f"tunnel:{e.kind}")
            e.route = "tunnel"
            raise
        else:
            attempts.append("tunnel:ok")
            return payload
    try:
        payload = _post_once(chunk_path, model, language,
                             timeout, None)
    except GroqError as e:
        attempts.append(f"direct:{e.kind}")
        e.route = "direct"
        raise
    attempts.append("direct:ok")
    return payload


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
    failure — the caller falls back to local whisper. On
    failure the breadcrumb additionally records the decisive
    attempt's route and a compact per-attempt outcome list
    (build 263)."""
    _attempts().clear()
    try:
        return _transcribe_audio(wav_path, Path(workdir), language)
    except GroqError as e:
        detail = f"{e.kind} {e.detail}"[:170]
        att = ",".join(_attempts())
        if att:
            detail += f" | attempts: {att}"
        _note_status(False, detail[:260],
                     route=getattr(e, "route", ""))
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
    ok_routes = []
    for a in _attempts():
        r, _, outcome = a.partition(":")
        if outcome == "ok" and r not in ok_routes:
            ok_routes.append(r)
    route = "+".join(ok_routes)
    _note_status(True, model, route=route)
    return {"segments": segments, "words": words,
            "language": language_out, "duration": round(duration_out, 3),
            "model": model, "chunks": len(wins), "route": route}


if __name__ == "__main__":
    print("groq key configured:", key_configured(),
          "| model:", get_model(), "| last:", last_status())
