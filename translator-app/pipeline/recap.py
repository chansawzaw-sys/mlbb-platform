"""Recap script generation (build 211) — the Recap product's
brain: transcript in, a Burmese recap narration script out.

The script is written by the SAME LLM gateways the translator
uses, walked in the SAME order as translate_segments (Gemini
leads while its probe verdict is green, then the free-first
gateways OpenRouter -> xKiro, then Gemini's fallback slot,
then OpenAI; region-blocked engines are probe-skipped exactly
as in translation). Each gateway's generate_text() reuses its
own model walk, so the winner logic and the free-first policy
carry over untouched. The translation-only routes (Apps
Script bridge, the free gtx chain) cannot GENERATE prose and
are not part of this chain.

Doctrine matches translation: if EVERY gateway fails (or
answers junk), generate_script raises — the job fails loudly
and is never charged. There is no silent fallback script.

Sizing: the narration should cover the whole video at natural
Burmese narration pace — ~700 chars per video minute (the
measured 650-800 chars/min band), stated to the model as a
target with a ±20% window. The render step still fits the
actual speech to the video with the retime machinery, so the
target is a guide, not a hard contract.

One request carries the whole transcript; transcripts longer
than TRANSCRIPT_CAP chars are truncated (a multi-hour video's
recap would need chunked generation — out of scope here; the
cap is far beyond the social-video lengths this product
targets).
"""
import re
from pathlib import Path

from . import (gemini_translate, openai_translate,
               openrouter_translate, translate_segments,
               xkiro_translate)
from .common import load_segments, probe_duration

CHARS_PER_MIN = 700          # narration pace (matches the studio)
TRANSCRIPT_CAP = 100_000     # chars of transcript per request
SCRIPT_MIN_RATIO = 0.25      # a reply shorter than target x this
                             # counts as a failed generation

PROMPT_RECAP = """You are a Burmese recap narrator. Below is the full transcript of a video. Write a Burmese recap narration script that retells the WHOLE video from start to finish (အစအဆုံး), in the same order as the video, in natural spoken Burmese (ပြောစကား) — engaging recap style, like a storyteller recounting the story so a listener who never saw the video understands everything that happened.

Rules:
- Cover the whole story in order — beginning, middle and end. Do not summarise away the ending, and do not invent anything that is not in the transcript.
- NAMES IN ENGLISH: every personal name, place name and brand name stays in Latin letters exactly as in the transcript (Chinese names in pinyin) — never write names in Burmese script, never translate a name by its meaning.
- Numbers must be exact — copy every number, score, date and amount precisely.
- STORY-FIRST: read the whole transcript first, understand the whole story, then write flowing narration that follows the storyline — never word-by-word translation, never a list of disconnected lines.
- NO BRACKETED GLOSSES: every name or term appears once, in a single chosen form — no parenthetical explanations, no translator notes, no markdown, no headings, no quotation marks around the script.
- Write ONLY the narration script itself, as plain paragraphs of spoken Burmese, ready to be read aloud.

Length: the video is about __MINUTES__ minutes long. Write about __TARGET__ Burmese characters (between __LOW__ and __HIGH__) so the narration fills the video at a natural pace.

Transcript:
__TRANSCRIPT__
"""


def _prompt(transcript, minutes, target):
    return (PROMPT_RECAP
            .replace("__MINUTES__", f"{minutes:.1f}")
            .replace("__TARGET__", str(int(target)))
            .replace("__LOW__", str(int(target * 0.8)))
            .replace("__HIGH__", str(int(target * 1.2)))
            .replace("__TRANSCRIPT__", transcript))


def _clean(text):
    """Strip code fences / wrapper prose markers an engine may
    add around the script; the script itself is plain Burmese
    paragraphs."""
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


def _usable(script, target):
    """A generation counts only if it is real Burmese narration
    of plausible size: long enough (a stub reply fails over to
    the next gateway) and mostly Myanmar script (names stay in
    Latin, so the prose around them must carry the ratio)."""
    if not script or len(script) < max(120, target * SCRIPT_MIN_RATIO):
        return False
    letters = [ch for ch in script if ch.isalpha()]
    if not letters:
        return False
    my = sum(1 for ch in letters if "\u1000" <= ch <= "\u109f")
    return my / len(letters) >= 0.35


def _transcript_text(workdir):
    segs = load_segments(Path(workdir) / "segments_en.json")
    parts = [(s.get("text") or "").strip() for s in segs]
    text = " ".join(p for p in parts if p)
    return text[:TRANSCRIPT_CAP]


def generate_script(workdir, target_chars):
    """Walk the gateways for a recap script of ~target_chars.
    Returns (script, provider_label). Raises RuntimeError with
    a polite Burmese message when every gateway fails — the
    caller fails the job (never charged)."""
    transcript = _transcript_text(workdir)
    if not transcript:
        raise RuntimeError(
            "ဗီဒီယိုထဲမှာ စကားသံ မတွေ့ပါ — recap လုပ်ဖို့ "
            "စာသား မရှိပါ (credit လုံးဝ မဖြတ်ပါ)")
    minutes = target_chars / CHARS_PER_MIN
    prompt = _prompt(transcript, minutes, target_chars)
    errors = {}

    def _try(name, fn, label_fn):
        try:
            out = _clean(fn(prompt))
            if _usable(out, target_chars):
                return out, label_fn()
            errors[name] = "unusable reply"
        except Exception as e:
            errors[name] = str(e)[:100]
        return None

    # Gemini lead slot — only while its probe verdict is green,
    # exactly like translate_segments (build 189).
    gemini_tried = False
    if translate_segments._probe_engine_ok("gemini"):
        gemini_tried = True
        if gemini_translate.key_configured():
            r = _try("gemini", gemini_translate.generate_text,
                     lambda: "gemini")
            if r:
                return r
    # Free-first gateways, in the translator's order.
    if openrouter_translate.key_configured():
        r = _try("openrouter", openrouter_translate.generate_text,
                 openrouter_translate.last_label)
        if r:
            return r
    if xkiro_translate.key_configured():
        r = _try("xkiro", xkiro_translate.generate_text,
                 xkiro_translate.last_label)
        if r:
            return r
    # Gemini's historical fallback slot (skipped when the probe
    # says region-blocked, or when the lead slot already tried).
    if not gemini_tried \
            and not translate_segments._probe_blocked("gemini") \
            and gemini_translate.key_configured():
        r = _try("gemini", gemini_translate.generate_text,
                 lambda: "gemini")
        if r:
            return r
    if not translate_segments._probe_blocked("openai") \
            and openai_translate.key_configured():
        r = _try("openai", openai_translate.generate_text,
                 lambda: "openai")
        if r:
            return r
    detail = "; ".join(f"{k}: {v}" for k, v in errors.items())
    raise RuntimeError(
        "Recap စာသား ရေးတာ မရပါ — AI service တွေ ခဏ "
        "အလုပ်မလုပ်ပါ။ ခဏနေမှ ပြန်စမ်းပါ (credit လုံးဝ မဖြတ်ပါ)"
        + (f" [{detail[:300]}]" if detail else ""))


def split_script(text, limit=450):
    """Split a recap script into TTS-sized chunks: sentence
    boundaries first (Burmese ။, !, ?, …, '.'+space, newlines),
    then greedy packing up to `limit` chars; a single over-long
    sentence is hard-split at a space when one is near. The
    same algorithm as the studio's chunker (app._tts_chunks),
    kept here so the pipeline never imports the app."""
    parts = [p.strip() for p in
             re.split(r"(?<=[။!?…])\s*|\n+|(?<=\.)\s+", text or "")]
    parts = [p for p in parts if p]
    units = []
    for p in parts:
        while len(p) > limit:
            cut = p.rfind(" ", 0, limit)
            if cut < limit // 2:
                cut = limit
            units.append(p[:cut].strip())
            p = p[cut:].strip()
        if p:
            units.append(p)
    chunks, cur = [], ""
    for u in units:
        if cur and len(cur) + 1 + len(u) > limit:
            chunks.append(cur)
            cur = u
        else:
            cur = f"{cur} {u}".strip() if cur else u
    if cur:
        chunks.append(cur)
    return chunks


def script_segments(text, total_dur):
    """Timed segments for the render: the script's chunks laid
    over the video's full duration, each window proportional
    to its chunk's share of the script's characters. The TTS
    pass measures the real speech lengths and retime fits them
    into these windows (speed-up capped, freeze-frame fill) —
    the same contract a dub's translated segments arrive
    with."""
    chunks = split_script(text)
    if not chunks or total_dur <= 0:
        return []
    total_chars = sum(len(c) for c in chunks)
    segs, t = [], 0.0
    for c in chunks:
        dur = total_dur * len(c) / total_chars
        segs.append({"start": round(t, 3),
                     "end": round(t + dur, 3),
                     "text": "", "text_my": c})
        t += dur
    if segs:
        segs[-1]["end"] = round(float(total_dur), 3)
    return segs


def target_chars_for(workdir):
    """The script size target for the job's source video:
    started minutes are NOT used here — the narration pace is
    per real minute of video (~700 chars/min)."""
    dur = probe_duration(Path(workdir) / "source.mp4")
    return max(CHARS_PER_MIN, round(dur / 60.0 * CHARS_PER_MIN))
