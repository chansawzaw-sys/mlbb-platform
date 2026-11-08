"""Step 4: Myanmar voice dub per segment — free, no API key.

Providers (first success wins; if edge-tts fails once in a job it is skipped
for the rest of that job):
  1. edge-tts — Microsoft neural voices. my-MM voices (verified 2026-11-07
     via `edge-tts --list-voices`):
       my-MM-NilarNeural  (female)
       my-MM-ThihaNeural  (male)
  2. Google Translate TTS, classic translate_tts endpoint (client=tw-ob),
     lang='my' — answers from IPs where gTTS's endpoint is 429-limited.
  3. gTTS — Google Translate TTS, lang='my' (one unnamed voice), retried
     with backoff across tld hosts.

Sandbox note (2026-11-07): this sandbox's egress proxy breaks edge-tts's
websocket handshake ("101 Invalid connection header"), so here the gTTS
fallback is what actually speaks; on a normal connection edge-tts is primary.
The provider used is recorded per segment as seg["tts_provider"].

CLI: python -m pipeline.tts <workdir> [voice]
Reads segments_my.json, writes tts/seg_XXX.mp3 per segment and sets
seg["tts_file"], seg["tts_duration"], seg["tts_provider"] in segments_my.json.
"""
import asyncio
import os
import sys
from pathlib import Path

import edge_tts

from .common import ensure_system_ca, load_segments, probe_duration, save_segments

DEFAULT_VOICE = "my-MM-NilarNeural"
FALLBACK_VOICES = ["my-MM-NilarNeural", "my-MM-ThihaNeural"]

# Voice tuning (2026-11-08): rate/pitch apply to edge-tts only — the
# Google/gTTS fallbacks have no such knob and speak at default speed/pitch
# (retime still syncs timing afterwards via atempo). Defaults are +0%/+0Hz,
# i.e. exactly the previous behaviour. Ranges are edge-tts's own limits:
# rate -50%..+100%, pitch -50Hz..+50Hz.


def _clamp(value, lo, hi):
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return 0


async def _edge_synth(text, voice, out_path, rate=0, pitch=0):
    proxy = (os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
             or os.environ.get("http_proxy") or None)
    await edge_tts.Communicate(
        text, voice, rate=f"{rate:+d}%", pitch=f"{pitch:+d}Hz",
        proxy=proxy).save(str(out_path))


def _classic_synth(text, out_path):
    """Google Translate TTS via the classic translate_tts endpoint.

    gTTS's batchexecute endpoint gets HTTP 429 from some IPs while this
    older `client=tw-ob` endpoint still answers — observed 2026-11-07 in
    this sandbox (429 on every gTTS host, 200 + real audio here).
    Text is split on sentence/space boundaries into <=180-char requests.
    """
    import re
    import urllib.parse

    import requests

    parts = [p for p in re.split(r"(?<=။)\s*|\s+", text) if p]
    chunks, cur = [], ""
    for p in parts:
        if len(cur) + len(p) + 1 > 180:
            if cur:
                chunks.append(cur)
            cur = p
        else:
            cur = f"{cur} {p}".strip()
    if cur:
        chunks.append(cur)
    audio = b""
    for ch in chunks:
        url = ("https://translate.google.com/translate_tts?ie=UTF-8"
               "&client=tw-ob&tl=my&q=" + urllib.parse.quote(ch))
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        r.raise_for_status()
        if "audio" not in r.headers.get("Content-Type", ""):
            raise RuntimeError("classic TTS returned non-audio")
        audio += r.content
    Path(out_path).write_bytes(audio)


_GTTS_TLDS = ["com", "com.au", "co.uk", "ca"]


def _gtts_synth(text, out_path):
    """gTTS with retry/backoff across Google Translate hosts.

    The free endpoint rate-limits per host/IP (HTTP 429) in bursts; rotating
    tld hosts and backing off rides out those windows instead of failing
    the whole job on the first 429.
    """
    import time

    from gtts import gTTS

    last_err = None
    for attempt in range(10):
        tld = _GTTS_TLDS[attempt % len(_GTTS_TLDS)]
        try:
            gTTS(text, lang="my", tld=tld).save(str(out_path))
            if Path(out_path).stat().st_size > 0:
                return
        except Exception as e:  # 429 etc — back off and try another host
            last_err = e
        time.sleep(min(6 * (attempt + 1), 30))
    raise RuntimeError(f"gTTS failed after retries: {last_err}")


def synthesize(workdir, voice=DEFAULT_VOICE, rate=0, pitch=0):
    """rate: speech-speed adjustment in percent (-50..+100, 0 = default).
    pitch: pitch adjustment in Hz (-50..+50, 0 = default). Both reach
    edge-tts only; fallback providers ignore them."""
    rate = _clamp(rate, -50, 100)
    pitch = _clamp(pitch, -50, 50)
    ensure_system_ca()
    workdir = Path(workdir)
    segs = load_segments(workdir / "segments_my.json")
    tts_dir = workdir / "tts"
    tts_dir.mkdir(exist_ok=True)
    edge_down = False
    for i, seg in enumerate(segs):
        out = tts_dir / f"seg_{i:03d}.mp3"
        text = (seg.get("text_my") or "").strip()
        if not text:
            continue
        provider = None
        if not edge_down:
            try:
                asyncio.run(_edge_synth(text, voice, out, rate, pitch))
                if out.exists() and out.stat().st_size > 0:
                    provider = f"edge-tts:{voice}"
            except Exception:
                edge_down = True  # don't pay the failure cost per segment
        if provider is None:
            try:
                _classic_synth(text, out)
                if out.exists() and out.stat().st_size > 0:
                    provider = "google-classic:my"
            except Exception:
                provider = None
        if provider is None:
            _gtts_synth(text, out)
            provider = "gtts:my"
        seg["tts_file"] = str(out)
        seg["tts_duration"] = round(probe_duration(out), 3)
        seg["tts_provider"] = provider
        seg["tts_rate"] = rate
        seg["tts_pitch"] = pitch
    save_segments(segs, workdir / "segments_my.json")
    return segs


if __name__ == "__main__":
    v = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_VOICE
    r = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    p = int(sys.argv[4]) if len(sys.argv) > 4 else 0
    res = synthesize(Path(sys.argv[1]), v, rate=r, pitch=p)
    provs = {}
    for r in res:
        provs[r.get("tts_provider")] = provs.get(r.get("tts_provider"), 0) + 1
    print(f"{len(res)} segments synthesized, providers: {provs}")
