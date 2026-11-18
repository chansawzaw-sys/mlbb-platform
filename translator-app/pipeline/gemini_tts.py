"""Gemini TTS voices (build 229) — Google's 30 prebuilt
voices join the picker as a STANDARD-rate group.

The owner auditioned Gemini speech generation in AI Studio
(2026-11-18) and ordered every voice in. The model is
gemini-3.8-flash-tts (the current Flash TTS in AI Studio;
GEMINI_TTS_MODEL in .env can override it) — the 2.5 preview
TTS was no longer listed there. The voices are Google's 30
prebuilt names; a job carries the pick as voice = "gm:<Name>".

Calls ride the SAME transport as Gemini translation: the
Singapore tunnel via tunnel.request_kwargs, and keys come
from the shared pool (pipeline.gemini_pool) — a quota/rate
failure cools the key down and retries the segment with the
next key; a key-invalid failure disables that key for good;
the job fails only when every key is exhausted.

Failure rule (the ElevenLabs rule): NO silent fallback. If
no key is configured, the voice name is unknown, or every
key fails, synthesize() raises a polite Burmese RuntimeError
and the job fails — never charged (charge-on-success),
never silently re-voiced with another engine.

Billing: the GEMINI_CREDITS_PER_MIN setting (default 1
credit per started minute — credits_per_min() below,
admin-adjustable like the ElevenLabs rate). Gemini voices
are NOT "premium" in the picker's sense: entries carry a
"rate" but no "premium" flag, and app.py's rate helper
bills "gm:" picks at this rate everywhere (Commentary:
its standard per-minute rate × this rate).

Speed: the Gemini API has no rate parameter, so the slider
lands as an atempo post-pass over the synthesized segments
(the build-228 pattern for ElevenLabs), applied inside
synthesize() when the caller passes rate != 0 (Story);
the Studio applies its own speed on the assembled audio,
and the video products pass voice_rate 0 for engine picks,
exactly like "el:".

Previews are LAZY: a voice's clip (the shared SAMPLE_TEXT)
is synthesized on the FIRST play request and cached as
voice_previews/gm-<Name>.mp3 — never bulk-generated (the
free-tier quota is precious; the owner has ~30 voices but
only plays a few).
"""
import base64
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from . import gemini_pool, tunnel
from .common import (ensure_system_ca, load_segments,
                     probe_duration, save_segments)
from . import elevenlabs_tts as _el
from .elevenlabs_tts import SAMPLE_TEXT

GM_PREFIX = "gm:"
DEFAULT_MODEL = "gemini-3.8-flash-tts"
_API_TMPL = ("https://generativelanguage.googleapis.com/"
             "v1beta/models/{model}:generateContent")
_REQUEST_TIMEOUT = 120

FAIL_MSG = ("အသံ ထုတ်တာ မရပါ — Gemini service ခဏ အလုပ်မလုပ်ပါ။ "
            "ခဏနေမှ ပြန်စမ်းပါ (credit လုံးဝ မဖြတ်ပါ)")

# Role keys shared with the picker's filter chips (app.py /
# index.html use the same vocabulary): narration ပုံပြင်,
# horror ကြောက်စရာ, sports အားကစား, news သတင်း,
# cheerful ပျော်ရွှင်, kids ကလေး.
ROLES = ("narration", "horror", "sports", "news",
         "cheerful", "kids")

# The 30 prebuilt Gemini voices. Gender + the one-word style
# characteristic are Google's published voice data; tag/desc
# are the picker's Burmese labels; roles map the published
# characteristic to the picker filters (an excitable voice
# is a sports voice, an informative one a news voice...).
VOICES = [
    {"name": "Achernar", "gender": "f", "tag": "နူးညံ့",
     "desc": "မိန်းကလေး အသံ — နူးညံ့ပြီး အေးဆေးတဲ့ လေသံ",
     "desc_en": "Female — soft, calm delivery",
     "roles": ["narration", "kids"]},
    {"name": "Achird", "gender": "m", "tag": "ဖော်ရွေ",
     "desc": "ယောက်ျား အသံ — ဖော်ရွေပြီး နွေးထွေးတဲ့ လေသံ",
     "desc_en": "Male — friendly, warm delivery",
     "roles": ["narration", "cheerful"]},
    {"name": "Algenib", "gender": "m", "tag": "အသံထူ",
     "desc": "ယောက်ျား အသံ — ထူထဲကြမ်းတမ်းတဲ့ လေသံ၊ ကြောက်စရာ ဇာတ်ကြောင်း အတွက်",
     "desc_en": "Male — gravelly voice, good for horror narration",
     "roles": ["horror", "narration"]},
    {"name": "Algieba", "gender": "m", "tag": "ချောမွေ့",
     "desc": "ယောက်ျား အသံ — ချောမွေ့ပြီး တည်ငြိမ်တဲ့ လေသံ",
     "desc_en": "Male — smooth, steady delivery",
     "roles": ["narration"]},
    {"name": "Alnilam", "gender": "m", "tag": "ခိုင်မာ",
     "desc": "ယောက်ျား အသံ — ခိုင်မာပြီး အာဏာပါတဲ့ လေသံ",
     "desc_en": "Male — firm, authoritative delivery",
     "roles": ["narration", "news"]},
    {"name": "Aoede", "gender": "f", "tag": "ပေါ့ပါး",
     "desc": "မိန်းကလေး အသံ — ပေါ့ပါးပြီး သဘာဝကျတဲ့ လေသံ",
     "desc_en": "Female — breezy, natural delivery",
     "roles": ["narration", "cheerful"]},
    {"name": "Autonoe", "gender": "f", "tag": "ကြည်လင်",
     "desc": "မိန်းကလေး အသံ — ကြည်လင်ပြီး တက်ကြွတဲ့ လေသံ",
     "desc_en": "Female — bright, lively delivery",
     "roles": ["cheerful", "news"]},
    {"name": "Callirrhoe", "gender": "f", "tag": "အေးဆေး",
     "desc": "မိန်းကလေး အသံ — အေးဆေးပြီး သက်တောင့်သက်သာရှိတဲ့ လေသံ",
     "desc_en": "Female — easy-going, relaxed delivery",
     "roles": ["narration", "cheerful"]},
    {"name": "Charon", "gender": "m", "tag": "သတင်းပုံစံ",
     "desc": "ယောက်ျား အသံ — သတင်းဖတ်သလို ရှင်းလင်းတဲ့ လေသံ",
     "desc_en": "Male — informative, newsreader delivery",
     "roles": ["news", "narration"]},
    {"name": "Despina", "gender": "f", "tag": "ချောမွေ့",
     "desc": "မိန်းကလေး အသံ — ချောမွေ့ပြီး နူးညံ့တဲ့ လေသံ",
     "desc_en": "Female — smooth, gentle delivery",
     "roles": ["narration"]},
    {"name": "Enceladus", "gender": "m", "tag": "လေသံပါ",
     "desc": "ယောက်ျား အသံ — လေသံပါပြီး ဆန်းကြယ်တဲ့ လေသံ၊ ကြောက်စရာ အတွက်",
     "desc_en": "Male — breathy, eerie delivery for horror",
     "roles": ["horror"]},
    {"name": "Erinome", "gender": "f", "tag": "ရှင်းလင်း",
     "desc": "မိန်းကလေး အသံ — ရှင်းလင်းပြီး သွက်လက်တဲ့ လေသံ",
     "desc_en": "Female — clear, crisp delivery",
     "roles": ["narration", "news"]},
    {"name": "Fenrir", "gender": "m", "tag": "စိတ်လှုပ်ရှား",
     "desc": "ယောက်ျား အသံ — စိတ်လှုပ်ရှား တက်ကြွတဲ့ လေသံ၊ အားကစား အတွက်",
     "desc_en": "Male — excitable, energetic delivery for sports",
     "roles": ["sports", "cheerful"]},
    {"name": "Gacrux", "gender": "f", "tag": "ရင့်ကျက်",
     "desc": "မိန်းကလေး အသံ — ရင့်ကျက်ပြီး တည်ကြည်တဲ့ လေသံ",
     "desc_en": "Female — mature, composed delivery",
     "roles": ["narration", "news"]},
    {"name": "Iapetus", "gender": "m", "tag": "ရှင်းလင်း",
     "desc": "ယောက်ျား အသံ — ရှင်းလင်းပြီး တိကျတဲ့ လေသံ",
     "desc_en": "Male — clear, precise delivery",
     "roles": ["narration", "news"]},
    {"name": "Kore", "gender": "f", "tag": "ခိုင်မာ",
     "desc": "မိန်းကလေး အသံ — ခိုင်မာပြီး တည်ကြည်တဲ့ လေသံ",
     "desc_en": "Female — firm, confident delivery",
     "roles": ["narration", "news"]},
    {"name": "Laomedeia", "gender": "f", "tag": "သွက်လက်",
     "desc": "မိန်းကလေး အသံ — သွက်လက်ပြီး ရွှင်လန်းတဲ့ လေသံ",
     "desc_en": "Female — upbeat, cheerful delivery",
     "roles": ["cheerful", "sports"]},
    {"name": "Leda", "gender": "f", "tag": "လူငယ်ဆန်း",
     "desc": "မိန်းကလေး အသံ — လူငယ်ဆန်ပြီး နုပျိုတဲ့ လေသံ၊ ကလေး အကြောင်းအရာ အတွက်",
     "desc_en": "Female — youthful voice, good for kids' content",
     "roles": ["kids", "cheerful"]},
    {"name": "Orus", "gender": "m", "tag": "ခိုင်မာ",
     "desc": "ယောက်ျား အသံ — ခိုင်မာပြီး နက်နဲတဲ့ လေသံ",
     "desc_en": "Male — firm, deep delivery",
     "roles": ["narration", "horror"]},
    {"name": "Pulcherrima", "gender": "f", "tag": "ရဲရင့်",
     "desc": "မိန်းကလေး အသံ — ရဲရင့်ပြီး ရှေ့တိုးတဲ့ လေသံ",
     "desc_en": "Female — forward, bold delivery",
     "roles": ["news", "sports"]},
    {"name": "Puck", "gender": "m", "tag": "သွက်လက်",
     "desc": "ယောက်ျား အသံ — သွက်လက်ပြီး ပျော်ရွှင်တဲ့ လေသံ",
     "desc_en": "Male — upbeat, playful delivery",
     "roles": ["cheerful", "sports"]},
    {"name": "Rasalgethi", "gender": "m", "tag": "သတင်းပုံစံ",
     "desc": "ယောက်ျား အသံ — သတင်းဖတ်သလို တိကျတဲ့ လေသံ",
     "desc_en": "Male — informative, precise delivery",
     "roles": ["news", "narration"]},
    {"name": "Sadachbia", "gender": "f", "tag": "ရွှင်လန်း",
     "desc": "မိန်းကလေး အသံ — ရွှင်လန်းပြီး တက်ကြွတဲ့ လေသံ",
     "desc_en": "Female — lively, animated delivery",
     "roles": ["cheerful", "kids"]},
    {"name": "Sadaltager", "gender": "m", "tag": "ပညာရှင်ဆန်း",
     "desc": "ယောက်ျား အသံ — ပညာရှင်ဆန်ပြီး စိတ်ချရတဲ့ လေသံ",
     "desc_en": "Male — knowledgeable, trustworthy delivery",
     "roles": ["news", "narration"]},
    {"name": "Schedar", "gender": "m", "tag": "တည်ငြိမ်",
     "desc": "ယောက်ျား အသံ — တည်ငြိမ်ပြီး ညီညာတဲ့ လေသံ",
     "desc_en": "Male — even, measured delivery",
     "roles": ["narration", "news"]},
    {"name": "Sulafat", "gender": "f", "tag": "နွေးထွေး",
     "desc": "မိန်းကလေး အသံ — နွေးထွေးပြီး စိတ်ချရတဲ့ လေသံ၊ ဇာတ်ကြောင်း အတွက်",
     "desc_en": "Female — warm delivery, lovely for narration",
     "roles": ["narration"]},
    {"name": "Umbriel", "gender": "m", "tag": "အေးဆေး",
     "desc": "ယောက်ျား အသံ — အေးဆေးပြီး သက်တောင့်သက်သာရှိတဲ့ လေသံ",
     "desc_en": "Male — easy-going, relaxed delivery",
     "roles": ["narration", "cheerful"]},
    {"name": "Vindemiatrix", "gender": "f", "tag": "နူးညံ့သိမ်မွေ့",
     "desc": "မိန်းကလေး အသံ — နူးညံ့သိမ်မွေ့ပြီး အေးချမ်းတဲ့ လေသံ",
     "desc_en": "Female — gentle, soothing delivery",
     "roles": ["narration", "kids"]},
    {"name": "Zephyr", "gender": "f", "tag": "ကြည်လင်",
     "desc": "မိန်းကလေး အသံ — ကြည်လင်ပြီး တောက်ပတဲ့ လေသံ",
     "desc_en": "Female — bright, sparkling delivery",
     "roles": ["cheerful", "news"]},
    {"name": "Zubenelgenubi", "gender": "m", "tag": "သာမန်",
     "desc": "ယောက်ျား အသံ — သာမန်ပြီး သဘာဝကျတဲ့ လေသံ",
     "desc_en": "Male — casual, natural delivery",
     "roles": ["cheerful", "narration"]},
]
VOICES_BY_NAME = {v["name"]: v for v in VOICES}


def _env_raw(name):
    v = (os.environ.get(name) or "").strip()
    if v:
        return v
    return gemini_pool._env_raw(name)


def get_model():
    m = _env_raw("GEMINI_TTS_MODEL")
    return m or DEFAULT_MODEL


DEFAULT_CREDITS_PER_MIN = 1


def credits_per_min():
    """The per-minute credit rate for Gemini voices — the
    GEMINI_CREDITS_PER_MIN setting (default 1), handled
    exactly like ELEVENLABS_CREDITS_PER_MIN: an integer
    1..100 read live from the env/.env, adjustable from the
    Admin Gemini card without a rebuild. The owner sets the
    number; the charge path (app.py's rate helper, the
    Studio, the bot pre-check) reads it here."""
    try:
        return max(1, min(100, int(
            _env_raw("GEMINI_CREDITS_PER_MIN")
            or DEFAULT_CREDITS_PER_MIN)))
    except (TypeError, ValueError):
        return DEFAULT_CREDITS_PER_MIN


def is_gm_voice(value):
    return bool(value) and str(value).startswith(GM_PREFIX)


def voice_name_of(value):
    return str(value)[len(GM_PREFIX):]


def find_voice(name):
    return VOICES_BY_NAME.get(str(name or ""))


def picker_entries():
    """Picker entries for /api/voices and the bot voice step.
    EMPTY unless at least one Gemini key is configured — an
    unconfigured install shows customers nothing new. Gemini
    voices bill the STANDARD rate, so entries carry no
    "premium" flag (every rate line in the app keys off it)."""
    if not gemini_pool.key_configured():
        return []
    rate = credits_per_min()
    return [{"id": GM_PREFIX + v["name"], "label": v["name"],
             "name": v["name"], "tag": v["tag"],
             "desc": v["desc"], "desc_en": v["desc_en"],
             "preview": preview_url_gm(v["name"]),
             "roles": list(v["roles"]), "gender": v["gender"],
             "gemini": True, "rate": rate} for v in VOICES]


# ---------- synthesis ----------

def _clean(text, keys):
    """Diagnostic snippets must never carry a pool key."""
    s = str(text or "")
    for k in keys:
        if k:
            s = s.replace(k, "***")
    return s


def _extract_pcm(resp):
    """The audio part of a generateContent reply -> (pcm
    bytes, sample rate). Raises ValueError when the reply
    carries no audio (a safety block, an empty candidate) —
    a content-determined failure no other key would fix."""
    data = resp.json()
    for cand in data.get("candidates") or []:
        for part in ((cand.get("content") or {}).get("parts")
                     or []):
            inline = part.get("inlineData") or {}
            if inline.get("data"):
                mime = str(inline.get("mimeType") or "")
                rate = 24000
                if "rate=" in mime:
                    try:
                        rate = int(mime.split("rate=")[1]
                                   .split(";")[0])
                    except (TypeError, ValueError):
                        rate = 24000
                return base64.b64decode(inline["data"]), rate
    raise ValueError("Gemini reply carried no audio part")


def _synth_pcm(voice_name, text):
    """One utterance -> raw PCM, walking the key pool: quota
    cools a key down and moves on, a key-invalid failure
    disables the key and moves on, a generic HTTP failure
    moves on too (another key's project may still serve);
    a network blip retries the same key once. Raises
    RuntimeError(FAIL_MSG) when every key is exhausted."""
    keys = gemini_pool.ordered_keys()
    if not keys:
        raise RuntimeError(
            "Gemini key မထည့်ရသေးပါ — admin က key ထည့်ပြီးမှ "
            "ဒီ အသံ သုံးလို့ရပါမယ် (credit လုံးဝ မဖြတ်ပါ)")
    model = get_model()
    url = _API_TMPL.format(model=model)
    body = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {
                "prebuiltVoiceConfig": {"voiceName": voice_name}}},
        },
    }
    last = None
    for key in keys:
        for attempt in range(2):
            try:
                resp = requests.post(
                    url, **tunnel.request_kwargs(
                        json=body, timeout=_REQUEST_TIMEOUT,
                        headers={"x-goog-api-key": key,
                                 "Content-Type":
                                     "application/json"}))
            except Exception as e:  # network blip — retry once
                last = _clean(e, keys)[:120]
                if attempt + 1 < 2:
                    time.sleep(1)
                continue
            if resp.status_code == 200:
                return _extract_pcm(resp)
            cls = gemini_pool.classify_error(resp.status_code,
                                             resp.text)
            if cls == "quota":
                gemini_pool.mark_cooling(key)
            elif cls == "invalid":
                gemini_pool.mark_disabled(key)
            last = f"HTTP {resp.status_code}"
            break  # any HTTP failure: next key
    raise RuntimeError(FAIL_MSG + (f" [{last}]" if last else ""))


def _pcm_to_mp3(pcm, rate, out_path):
    tmp = Path(str(out_path) + ".pcm")
    tmp.write_bytes(pcm)
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "s16le",
             "-ar", str(rate), "-ac", "1", "-i", str(tmp),
             "-c:a", "libmp3lame", "-q:a", "3", str(out_path)],
            check=True, timeout=120)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _apply_rate(workdir, rate):
    """The speed slider for Gemini narration (build 229):
    the API has no rate parameter, so — the build-228
    ElevenLabs pattern — the rate lands as ONE atempo pass
    over each synthesized segment and durations are
    re-measured, keeping the caller's timeline in sync.
    rate is percent (-50..+100) -> factor 0.5..2.0."""
    try:
        rate = int(rate or 0)
    except (TypeError, ValueError):
        rate = 0
    if not rate:
        return
    factor = min(2.0, max(0.5, 1.0 + rate / 100.0))
    seg_path = Path(workdir) / "segments_my.json"
    segs = load_segments(seg_path)
    changed = False
    for seg in segs:
        f = seg.get("tts_file")
        if not f or not Path(f).exists():
            continue
        tmp = str(f) + ".rate.mp3"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(f),
             "-filter:a", f"atempo={factor:.4f}", tmp],
            check=True, timeout=300)
        Path(tmp).replace(f)
        seg["tts_duration"] = round(probe_duration(f), 3)
        changed = True
    if changed:
        save_segments(segs, seg_path)


def synthesize(workdir, voice_name, rate=0):
    """Per-segment Gemini synthesis with the SAME output
    contract as pipeline.tts / elevenlabs_tts.synthesize:
    writes tts/seg_XXX.mp3 and sets tts_file / tts_duration /
    tts_provider (+ tts_engine / tts_voice / tts_voice_name)
    on every segment of segments_my.json. `rate` (the Story
    speed slider, percent) is applied as an atempo pass after
    synthesis. Any failure raises — the job fails loudly and
    is never charged; there is no fallback voice."""
    entry = find_voice(voice_name)
    if entry is None:
        raise RuntimeError(
            "ရွေးထားတဲ့ Gemini အသံ မရှိတော့ပါ — အသံ ပြန်ရွေးပေးပါ "
            "(credit လုံးဝ မဖြတ်ပါ)")
    if not gemini_pool.key_configured():
        raise RuntimeError(
            "Gemini key မထည့်ရသေးပါ — admin က key ထည့်ပြီးမှ "
            "ဒီ အသံ သုံးလို့ရပါမယ် (credit လုံးဝ မဖြတ်ပါ)")
    ensure_system_ca()
    workdir = Path(workdir)
    segs = load_segments(workdir / "segments_my.json")
    tts_dir = workdir / "tts"
    tts_dir.mkdir(exist_ok=True)
    jobs = [(i, (seg.get("text_my") or "").strip(),
             tts_dir / f"seg_{i:03d}.mp3")
            for i, seg in enumerate(segs)]
    jobs = [(i, t, o) for i, t, o in jobs if t]
    if jobs:
        def _one(job_args):
            _i, _t, _o = job_args
            pcm, rate_hz = _synth_pcm(entry["name"], _t)
            _pcm_to_mp3(pcm, rate_hz, _o)
        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(_one, jobs))
    model = get_model()
    for i, text, out in jobs:
        seg = segs[i]
        seg["tts_file"] = str(out)
        seg["tts_duration"] = round(probe_duration(out), 3)
        seg["tts_provider"] = f"gemini:{model}"
        seg["tts_engine"] = "gemini"
        seg["tts_voice"] = entry["name"]
        seg["tts_voice_name"] = entry["name"]
    save_segments(segs, workdir / "segments_my.json")
    if rate:
        _apply_rate(workdir, rate)
    return segs


# ---------- lazy previews ----------

def preview_filename_gm(name):
    return f"gm-{name}.mp3"


def preview_path_gm(name):
    # Read the directory off the module at CALL time (not an
    # import-time copy): the app and the proofs redirect
    # elevenlabs_tts.PREVIEW_DIR, and the gm- clips must land
    # in the same directory the route serves from.
    return _el.PREVIEW_DIR / preview_filename_gm(name)


def preview_url_gm(name):
    """The clip's URL — ALWAYS the convention URL (like the
    built-in std- clips): the file itself is generated
    lazily by the /voice-previews route on first request."""
    return f"/voice-previews/{preview_filename_gm(name)}"


def generate_preview(name):
    """Best-effort preview synthesis -> PREVIEW_DIR/gm-<Name>
    .mp3 (one fixed sample line, one pool key walk). Returns
    True when the clip exists afterwards. NEVER raises: no
    key / API failure just leaves the voice without a clip
    (unlike a job, which must fail loud)."""
    try:
        if not find_voice(name) or not gemini_pool.key_configured():
            return False
        _el.PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        path = preview_path_gm(name)
        tmp = path.with_name(path.name + ".tmp.mp3")
        try:
            pcm, rate_hz = _synth_pcm(name, SAMPLE_TEXT)
            _pcm_to_mp3(pcm, rate_hz, tmp)
            tmp.replace(path)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
        return path.exists()
    except Exception:
        return False
