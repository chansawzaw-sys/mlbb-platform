"""Recap script generation (build 211) — the Recap product's
brain: transcript in, a Burmese recap narration script out.

The script is written through the ONE shared generation
walk, pipeline/generate_chain.chain_generate. Build 269
(owner decision 2026-11-27, mirroring build 264 for
translation): generation is OpenAI-only by default — the
pre-269 gateway walk (Gemini lead, OpenRouter -> xKiro,
Gemini fallback slot, OpenAI) survives dormant inside
generate_chain behind GENERATE_CHAIN=legacy. The
translation-only routes (Apps Script bridge, the free gtx
chain) cannot GENERATE prose and were never part of this
chain.

Doctrine matches translation: if the active chain fails
(or answers junk), generate_script raises — the job fails
loudly and is never charged. There is no silent fallback
script.

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

Build 270 — the Reclivo-style FULL mode (owner approved
2026-11-27 after studying Reclivo 2's full-retelling manhwa
recaps): a second narration style on the same product,
job field "recap_style" ("summary" = the build-211 mode,
unchanged in prompt, sizing and usability floor; "full" =
PROMPT_RECAP_FULL). Full mode is a complete spoken retelling
— every scene in order, every character's dialogue performed
in-line by the one narrator, colloquial spoken Burmese,
relationship pronouns, names transliterated (the standing
build-252 rule), money/units kept in their original form.
Its size target is denser (FULL_CHARS_PER_MIN) and anchored
to the source transcript's own length when that is larger —
a full retelling carries the story's whole spoken content,
not a summary's selection — and its usability floor is
stricter (FULL_MIN_RATIO), so a compressed summary-style
reply counts as junk instead of shipping as a "full"
retelling. Generation goes through the same shared chain
(build 269) either way; pricing is untouched (the quote is
started source minutes x the voice rate, style-blind).
"""
import re
from pathlib import Path

from . import generate_chain
from .common import load_segments, probe_duration

CHARS_PER_MIN = 700          # narration pace (matches the studio)
TRANSCRIPT_CAP = 100_000     # chars of transcript per request
SCRIPT_MIN_RATIO = 0.25      # a reply shorter than target x this
                             # counts as a failed generation
FULL_CHARS_PER_MIN = 900     # build 270: the full retelling's
                             # denser pace — dialogue performed
                             # in-line leaves fewer silent gaps
                             # than a summary narration
FULL_MIN_RATIO = 0.6         # build 270: a full-mode reply
                             # shorter than target x this is a
                             # compressed summary, not a full
                             # retelling — it counts as junk
RECAP_STYLES = ("summary", "full")

PROMPT_RECAP = """You are a Burmese recap narrator. Below is the full transcript of a video. Write a Burmese recap narration script that retells the WHOLE video from start to finish (အစအဆုံး), in the same order as the video, in natural spoken Burmese (ပြောစကား) — engaging recap style, like a storyteller recounting the story so a listener who never saw the video understands everything that happened.

Rules:
- Cover the whole story in order — beginning, middle and end. Do not summarise away the ending, and do not invent anything that is not in the transcript.
- NAMES IN BURMESE: every personal name, place name and brand name is written in Burmese script, transliterated by its SOUND as it sounds in the transcript (for a Chinese name, transliterate the pinyin sound — Lin Feng becomes လင်ဖုန်း) — never leave a name in Latin letters, never translate a name by its meaning, and use ONE fixed Burmese form for each name every time it appears.
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


PROMPT_RECAP_FULL = """You are a Burmese storyteller-narrator. Below is the full transcript of a video. Retell the ENTIRE story from start to finish (အစအဆုံး) as one continuous spoken performance, in the same order as the video — a FULL retelling, never a summary.

Rules:
- NEVER summarise, never skip a scene, never compress events: cover every scene and every beat of the story in order, beginning, middle and end. Do not invent anything that is not in the transcript.
- PERFORM THE DIALOGUE IN-LINE: every character's dialogue is spoken AS the character, in-line inside the narration — you are the single narrator voice performing every part yourself. Never compress dialogue into indirect summaries like "he said that…" or "she told him to…" — speak the actual lines as the character would say them.
- COLLOQUIAL SPOKEN BURMESE (ပြောစကား) throughout — natural everyday speech as people really talk, never stiff written or formal style.
- PRONOUNS FOLLOW THE RELATIONSHIP between speaker and listener: ငါ/မင်း between close equals or in anger, အစ်ကို/ညီမလေး and other respectful family terms where the relationship calls for them — each character speaks to each other character the way their relationship demands.
- NAMES IN BURMESE: every personal name, place name and brand name is written in Burmese script, transliterated by its SOUND as it sounds in the transcript (for a Chinese name, transliterate the pinyin sound — Lin Feng becomes လင်ဖုန်း) — never leave a name in Latin letters, never translate a name by its meaning, and use ONE fixed Burmese form for each name every time it appears.
- MONEY AND UNITS STAY IN THE ORIGINAL FORM: amounts and units are written as Burmese words in their original unit — yuan is written ရွန် — never converted to kyat, never swapped for a Myanmar unit.
- Numbers must be exact — copy every number, score, date and amount precisely (in its original unit, per the rule above).
- STORY-FIRST: read the whole transcript first, understand the whole story and who everyone is to each other, then perform it as flowing spoken storytelling — never word-by-word translation, never a list of disconnected lines.
- NO BRACKETED GLOSSES: every name or term appears once, in a single chosen form — no parenthetical explanations, no translator notes, no markdown, no headings, no quotation marks around the script.
- Write ONLY the retelling itself, as plain paragraphs of spoken Burmese, ready to be read aloud by one voice.

Length: the video is about __MINUTES__ minutes long. This is a full retelling. Write about __TARGET__ Burmese characters (between __LOW__ and __HIGH__) — enough to carry the whole story with all of its dialogue, at a natural spoken pace.

Transcript:
__TRANSCRIPT__
"""


def _prompt_full(transcript, minutes, target):
    return (PROMPT_RECAP_FULL
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


def _usable(script, target, min_ratio=SCRIPT_MIN_RATIO):
    """A generation counts only if it is real Burmese narration
    of plausible size: long enough (a stub reply fails over to
    the next gateway) and mostly Myanmar script (names are
    Burmese transliterations since build 252, so the whole
    script — names included — carries the ratio). Build 270:
    full mode passes FULL_MIN_RATIO — its floor is stricter
    because a short reply there is a summary in disguise."""
    if not script or len(script) < max(120, target * min_ratio):
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


def generate_script(workdir, target_chars, style="summary"):
    """Generate a recap script of ~target_chars through the
    shared generation chain (build 269: OpenAI-only by
    default). `style` is the build-270 narration style:
    "summary" (the build-211 recap — prompt, sizing and
    usability floor exactly as before) or "full" (the
    Reclivo-style full retelling — PROMPT_RECAP_FULL and the
    stricter FULL_MIN_RATIO floor); any other value falls
    back to "summary". Returns (script, provider_label).
    Raises RuntimeError with a polite Burmese message when
    the chain fails — the caller fails the job (never
    charged)."""
    style = style if style in RECAP_STYLES else "summary"
    transcript = _transcript_text(workdir)
    if not transcript:
        raise RuntimeError(
            "ဗီဒီယိုထဲမှာ စကားသံ မတွေ့ပါ — recap လုပ်ဖို့ "
            "စာသား မရှိပါ (credit လုံးဝ မဖြတ်ပါ)")
    if style == "full":
        # The prompt's minutes figure is the VIDEO's length:
        # at the full pace, target / FULL_CHARS_PER_MIN
        # recovers it (approximately, when the target was
        # transcript-anchored).
        prompt = _prompt_full(transcript,
                              target_chars / FULL_CHARS_PER_MIN,
                              target_chars)

        def extract(raw):
            out = _clean(raw)
            return out if _usable(out, target_chars,
                                  FULL_MIN_RATIO) else None
    else:
        prompt = _prompt(transcript,
                         target_chars / CHARS_PER_MIN,
                         target_chars)

        def extract(raw):
            out = _clean(raw)
            return out if _usable(out, target_chars) else None

    # Build 269: the walk itself lives in
    # pipeline/generate_chain (OpenAI-only by default; the
    # pre-269 gateway walk is dormant there behind
    # GENERATE_CHAIN=legacy).
    try:
        return generate_chain.chain_generate(prompt, extract)
    except RuntimeError as e:
        detail = str(e)[:300]
        raise RuntimeError(
            "Recap စာသား ရေးတာ မရပါ — AI service တွေ ခဏ "
            "အလုပ်မလုပ်ပါ။ ခဏနေမှ ပြန်စမ်းပါ (credit လုံးဝ မဖြတ်ပါ)"
            + (f" [{detail}]" if detail else ""))


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


def target_chars_for(workdir, style="summary"):
    """The script size target for the job's source video:
    started minutes are NOT used here — the narration pace is
    per real minute of video (~700 chars/min). Build 270:
    "full" sizes the retelling at the denser FULL pace and
    never below the source transcript's own length (capped
    at TRANSCRIPT_CAP, the most the generation ever sees) —
    a full retelling carries the story's whole spoken
    content, so the source's own words are its size guide."""
    dur = probe_duration(Path(workdir) / "source.mp4")
    base = max(CHARS_PER_MIN, round(dur / 60.0 * CHARS_PER_MIN))
    if style != "full":
        return base
    target = max(FULL_CHARS_PER_MIN,
                 round(dur / 60.0 * FULL_CHARS_PER_MIN))
    transcript = _transcript_text(workdir)
    if transcript:
        target = max(target, min(len(transcript),
                                 TRANSCRIPT_CAP))
    return target
