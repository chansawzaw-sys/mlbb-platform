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
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import edge_tts

from .common import (ensure_system_ca, load_segments, probe_duration,
                     run, save_segments)

DEFAULT_VOICE = "my-MM-NilarNeural"
FALLBACK_VOICES = ["my-MM-NilarNeural", "my-MM-ThihaNeural"]

# Voice tuning (2026-11-08): rate/pitch apply to edge-tts only — the
# Google/gTTS fallbacks have no such knob and speak at default speed/pitch
# (retime still syncs timing afterwards via atempo). Defaults are +0%/+0Hz,
# i.e. exactly the previous behaviour. Ranges are edge-tts's own limits:
# rate -50%..+100%, pitch -50Hz..+50Hz.

VOICE_THIHA = "my-MM-ThihaNeural"
VOICE_NILAR = "my-MM-NilarNeural"

# Voice styles (build 200): the dub-voice picker on BOTH surfaces
# (web wizard + Telegram bot) offers STYLES, not raw voices — the
# two my-MM voices (Microsoft's whole Burmese catalogue), each in
# three pitch variants. The pitch step is ±8Hz, well inside the
# ±50Hz tuning clamp; rate stays 0 because speaking speed is the
# separate retime speed-cap knob, not a voice trait. This table is
# the ONE shared source: jobs carry the style KEY, and job creation
# resolves it here to the (voice, pitch, rate) the synth path
# consumes.
VOICE_STYLES = [
    {"key": "thiha", "label": "Thiha (မူလ)",
     "voice": VOICE_THIHA, "pitch": 0, "rate": 0,
     "name": "Thiha", "tag": "ပုံမှန်",
     "desc": "ယောက်ျား အသံ — မူလ လေသံ၊ ဇာတ်ကြောင်း ပြောရန် သင့်တော်"},
    {"key": "thiha-deep", "label": "Thiha အသံနက်",
     "voice": VOICE_THIHA, "pitch": -8, "rate": 0,
     "name": "Thiha", "tag": "အသံနက်",
     "desc": "Thiha အသံကို နိမ့်နက်အောင် လုပ်ထားတာ — တည်ငြိမ်တဲ့ ဇာတ်ကြောင်း အတွက်"},
    {"key": "thiha-light", "label": "Thiha အသံပါး",
     "voice": VOICE_THIHA, "pitch": 8, "rate": 0,
     "name": "Thiha", "tag": "အသံပါး",
     "desc": "Thiha အသံကို မြင့်ပေါ့အောင် လုပ်ထားတာ — ပေါ့ပါးတဲ့ အကြောင်းအရာ အတွက်"},
    {"key": "nilar", "label": "Nilar (မူလ)",
     "voice": VOICE_NILAR, "pitch": 0, "rate": 0,
     "name": "Nilar", "tag": "ပုံမှန်",
     "desc": "မိန်းကလေး အသံ — မူလ လေသံ၊ ဇာတ်ကြောင်း ပြောရန် သင့်တော်"},
    {"key": "nilar-deep", "label": "Nilar အသံနက်",
     "voice": VOICE_NILAR, "pitch": -8, "rate": 0,
     "name": "Nilar", "tag": "အသံနက်",
     "desc": "Nilar အသံကို နိမ့်နက်အောင် လုပ်ထားတာ — တည်ငြိမ်တဲ့ လေသံ အတွက်"},
    {"key": "nilar-high", "label": "Nilar အသံမြင့်",
     "voice": VOICE_NILAR, "pitch": 8, "rate": 0,
     "name": "Nilar", "tag": "အသံမြင့်",
     "desc": "Nilar အသံကို မြင့်အောင် လုပ်ထားတာ — ရွှင်လန်းတဲ့ အကြောင်းအရာ အတွက်"},
]
STYLE_BY_KEY = {s["key"]: s for s in VOICE_STYLES}
# The product default the owner settled on: Thiha, original pitch.
DEFAULT_STYLE = "thiha"


def resolve_style(value):
    """Resolve a style key OR a legacy plain voice id to a preset
    dict {key, label, voice, pitch, rate} (a copy — callers may not
    mutate the table). A plain voice id maps to that voice's မူလ
    (original) variant; anything unknown or empty fails safe to
    DEFAULT_STYLE."""
    if value:
        v = str(value).strip()
        hit = STYLE_BY_KEY.get(v)
        if hit:
            return dict(hit)
        for s in VOICE_STYLES:
            if s["voice"] == v and s["pitch"] == 0:
                return dict(s)
    return dict(STYLE_BY_KEY[DEFAULT_STYLE])


def style_label(value):
    """Display label for a style key / plain voice id (pickers,
    summaries) — resolves through the same table."""
    return resolve_style(value)["label"]


def style_library():
    """Voice-library metadata for the built-in styles (build
    207): persona name + style tag + one-line description per
    style, for the picker's library rows. The styles are pitch
    variants of the two my-MM voices — the descriptions say so
    honestly."""
    return [{"key": s["key"], "label": s["label"],
             "name": s.get("name", ""), "tag": s.get("tag", ""),
             "desc": s.get("desc", "")} for s in VOICE_STYLES]


def synthesize_sample(text, style_key, out_path):
    """One-shot synthesis of a short sample line in a built-in
    style (the picker's preview clips, build 207). Raises on
    failure — the caller decides what an absent preview means."""
    style = resolve_style(style_key)
    asyncio.run(_edge_synth(text, style["voice"], out_path,
                            rate=style["rate"], pitch=style["pitch"]))


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
    # Was 10 attempts with sleeps up to 30 s (~4 min of pure sleeping per
    # segment in the worst case) — this is the LAST fallback, so a long
    # tail here multiplies across every remaining segment. 4 attempts
    # with a short backoff rides out a 429 burst without the crawl.
    for attempt in range(4):
        tld = _GTTS_TLDS[attempt % len(_GTTS_TLDS)]
        try:
            gTTS(text, lang="my", tld=tld).save(str(out_path))
            if Path(out_path).stat().st_size > 0:
                return
        except Exception as e:  # 429 etc — back off and try another host
            last_err = e
        time.sleep(min(4 * (attempt + 1), 12))
    raise RuntimeError(f"gTTS failed after retries: {last_err}")


async def _edge_all(jobs, voice, rate, pitch):
    """Synthesize all segments with edge-tts, up to 4 at a time.

    The old loop ran one websocket session per segment back-to-back —
    with 20-30 segments that alone took a minute or more. Returns the
    set of segment indices that produced a non-empty file."""
    sem = asyncio.Semaphore(6)
    ok = set()

    async def one(idx, text, out):
        async with sem:
            try:
                # A hung websocket must not stall the whole step: cap each
                # segment at 60 s, then let the fallback chain take it.
                await asyncio.wait_for(
                    _edge_synth(text, voice, out, rate, pitch), timeout=60)
                if out.exists() and out.stat().st_size > 0:
                    ok.add(idx)
            except Exception:
                pass

    await asyncio.gather(*(one(i, t, o) for i, t, o in jobs))
    return ok


# Build 186 (audit fix #5): the classic/gTTS fallback used to run
# SEQUENTIALLY per segment — on an edge-down day every missing
# segment paid the full provider ladder one after another
# (measured mild case: ~1.14 s/seg; a hanging provider cost its
# whole retry ladder, ~2.5-3 min, PER SEGMENT). The fallback now
# runs across segments in parallel (same semaphore width as the
# edge path) and every segment carries a wall-clock budget.
SEG_BUDGET_S = 45.0    # per-segment ceiling for the whole fallback ladder
ATTEMPT_CAP_S = 30.0   # per-provider-attempt ceiling inside it (the
                       # classic endpoint's own request timeout is
                       # 30 s, so a healthy attempt is never cut)


def _run_capped(fn, cap_s):
    """Run fn() in a worker thread under a hard ceiling; True only
    if it finished without raising inside the cap. A timed-out
    attempt is abandoned (the thread is a daemon to the pool's
    shutdown(wait=False) — the same pattern translate_segments
    uses for its hung-provider guard) and counts as a failure."""
    ex = ThreadPoolExecutor(max_workers=1)
    try:
        ex.submit(fn).result(timeout=max(1.0, cap_s))
        return True
    except Exception:
        return False
    finally:
        ex.shutdown(wait=False)


def _fallback_for_segment(text, out):
    """The pre-186 fallback ladder for ONE segment — classic ->
    gTTS -> one breath -> classic -> gTTS — same providers, same
    order, same retry, now under SEG_BUDGET_S with each attempt
    capped at ATTEMPT_CAP_S of the remaining budget. Returns the
    provider label, or None when the ladder produced nothing
    (the caller then applies the recorded FAILED-silent last
    resort, exactly as before — a segment is never dropped and
    never silently voiceless: it is either spoken, or recorded
    FAILED-silent with the majority-failure job gate behind it).

    An abandoned (timed-out) attempt can leave a partial file —
    gTTS streams into its target — so the target is unlinked
    after every failed attempt; an abandoned writer holds an fd
    to the unlinked inode and cannot pollute the next attempt's
    file at the same path."""
    t0 = time.monotonic()

    def left():
        return SEG_BUDGET_S - (time.monotonic() - t0)

    def attempt(fn):
        if left() <= 1:
            return False
        ok = _run_capped(lambda: fn(text, out),
                         min(ATTEMPT_CAP_S, left()))
        good = ok and out.exists() and out.stat().st_size > 0
        if not good:
            try:
                out.unlink(missing_ok=True)
            except Exception:
                pass
        return good

    if attempt(_classic_synth):
        return "google-classic:my"
    if attempt(_gtts_synth):
        return "gtts:my"
    # One full-chain retry after a breath (transient network
    # failures are the usual cause) — kept from the pre-186
    # ladder, shortened to what the budget still affords.
    if left() > 3:
        time.sleep(min(2.0, left() - 1))
    if attempt(_classic_synth):
        return "google-classic:my (retry)"
    if attempt(_gtts_synth):
        return "gtts:my (retry)"
    return None


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
    jobs = [(i, (seg.get("text_my") or "").strip(), tts_dir / f"seg_{i:03d}.mp3")
            for i, seg in enumerate(segs)]
    jobs = [(i, t, o) for i, t, o in jobs if t]
    edge_ok = set()
    failed = []
    if jobs:
        try:
            edge_ok = asyncio.run(_edge_all(jobs, voice, rate, pitch))
        except Exception:
            edge_ok = set()
    # Build 186 (audit fix #5): segments edge missed run the
    # classic/gTTS fallback ladder CONCURRENTLY (6 workers, the
    # edge path's width) instead of one segment at a time; each
    # segment's own ladder order and budget live in
    # _fallback_for_segment. Finalization below stays in segment
    # order in this thread, so the recorded outcome per segment
    # (provider, FAILED-silent, the majority-failure gate) is
    # exactly the pre-186 contract.
    providers = {}
    missing = [(i, t, o) for i, t, o in jobs if i not in edge_ok]
    if missing:
        def _fb(job_args):
            _i, _t, _o = job_args
            return _fallback_for_segment(_t, _o)
        with ThreadPoolExecutor(max_workers=6) as ex:
            for (i, _t, _o), prov in zip(missing, ex.map(_fb, missing)):
                if prov:
                    providers[i] = prov
    for i, text, out in jobs:
        seg = segs[i]
        provider = None
        if i in edge_ok:
            provider = f"edge-tts:{voice}"
        if provider is None:
            provider = providers.get(i)
        if provider is None:
            # Last resort for THIS segment only: silence of the
            # segment's own window length. The timeline stays exact
            # and the subtitle still carries the line — and the
            # failure is RECORDED on the segment, never silent. If
            # most segments land here the job fails loudly below
            # instead of shipping a mostly-silent dub.
            dur = max(0.5, float(seg.get("end") or 0)
                      - float(seg.get("start") or 0))
            run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                 "-i", "anullsrc=r=44100:cl=stereo", "-t", f"{dur:.2f}",
                 str(out)])
            provider = "FAILED-silent"
            failed.append(i)
        seg["tts_file"] = str(out)
        seg["tts_duration"] = round(probe_duration(out), 3)
        seg["tts_provider"] = provider
        seg["tts_rate"] = rate
        seg["tts_pitch"] = pitch
    if jobs and len(failed) * 2 > len(jobs):
        raise RuntimeError(
            "အသံ ထုတ်တာ မရပါ — TTS service တွေ ခဏ အလုပ်မလုပ်ပါ။ "
            "ခဏနေမှ ပြန်စမ်းပါ (credit လုံးဝ မဖြတ်ပါ)")
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
