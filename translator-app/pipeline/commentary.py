"""Commentary generation + original-audio bed (build 217) —
the Commentary product's brain and its one render extra.

Brain: the video's own transcript in, an energetic Burmese
sports/commentary-style script out — play-by-play energy,
present tense, following the action in the video's own
order. The script is written by the SAME LLM gateways the
translator / Recap / Story use, walked in the SAME order
(story._chain_generate carries recap's seams verbatim:
Gemini leads while its probe verdict is green, then the
free-first gateways OpenRouter -> xKiro, then Gemini's
fallback slot, then OpenAI). Doctrine matches the other
products: if EVERY gateway fails (or answers junk),
generate_script raises — the job fails loudly and is never
charged. There is no silent fallback script.

Sizing mirrors Recap: ~700 Burmese chars per SOURCE minute
(the measured 650-800 chars/min narration band), stated to
the model as a target with a ±20% window. The render lays
the script over the video with recap's proportional
segments and the shared retime machinery fits the speech.

Bed: a commentary plays OVER the original picture, and the
customer chooses whether the original sound stays on —
ducked low under the narration (the default) or off
entirely. "Off" is simply the ordinary Recap render (the
retime's audio is narration-only by design since build
177). "Ducked" is a post-step here: retime persists its
chunk plan (retime_plan.json), so the source audio can be
rebuilt chunk by chunk at each chunk's own rate — the bed
stays in sync with the re-timed picture — then mixed under
dub.wav at BED_VOLUME and muxed over the finished video.
The bed is garnish, never the meal: any failure in here
leaves the narration-only output standing and the job
still succeeds (the same rule as Story's music bed).
"""
import json
from pathlib import Path

from . import recap, story
from .common import (load_segments, probe_duration, probe_streams,
                     run)

CHARS_PER_MIN = recap.CHARS_PER_MIN
TRANSCRIPT_CAP = recap.TRANSCRIPT_CAP
MIN_SEGMENTS = 3          # fewer usable transcript segments
                          # than this cannot carry a commentary
BED_VOLUME = 0.13         # the ducked bed: ~13% of the
                          # original volume under the narration

PROMPT_COMMENTARY = """You are a Burmese sports commentator. Below is the full transcript of a video. Write a Burmese commentary script that narrates the video AS IT HAPPENS — play-by-play style, high energy, like a live commentator calling the action while it unfolds on screen.

Rules:
- PRESENT TENSE, moment by moment, in the video's own order — call each event as it happens, build excitement toward the big moments, celebrate the highlights. Energetic spoken Burmese (ပြောစကား), the voice of a commentator, not a summary writer.
- Follow ONLY what is in the transcript — never invent plays, scores, names or events that are not there.
- NAMES IN BURMESE: every personal name, place name and brand name is written in Burmese script, transliterated by its SOUND as it sounds in the transcript (for a Chinese name, transliterate the pinyin sound — Lin Feng becomes လင်ဖုန်း) — never leave a name in Latin letters, never translate a name by its meaning, and use ONE fixed Burmese form for each name every time it appears.
- Numbers must be exact — copy every number, score, date and amount precisely.
- STORY-FIRST: read the whole transcript first, understand the whole flow of the video, then write flowing commentary that follows the action — never word-by-word translation, never a list of disconnected lines.
- NO BRACKETED GLOSSES: every name or term appears once, in a single chosen form — no parenthetical explanations, no translator notes, no markdown, no headings, no quotation marks around the script.
- Write ONLY the commentary script itself, as plain paragraphs of spoken Burmese, ready to be read aloud.

Length: the video is about __MINUTES__ minutes long. Write about __TARGET__ Burmese characters (between __LOW__ and __HIGH__) so the commentary fills the video at a natural pace.

Transcript:
__TRANSCRIPT__
"""

FAIL_MSG = ("Commentary စာသား ရေးတာ မရပါ — AI service တွေ ခဏ "
            "အလုပ်မလုပ်ပါ။ ခဏနေမှ ပြန်စမ်းပါ "
            "(credit လုံးဝ မဖြတ်ပါ)")

SPARSE_MSG = ("ဗီဒီယိုထဲမှာ စကားသံ နည်းလွန်းပါတယ် — "
              "Commentary လုပ်ဖို့ စာသား မလုံလောက်ပါ "
              "(credit လုံးဝ မဖြတ်ပါ)")


def _prompt(transcript, minutes, target):
    return (PROMPT_COMMENTARY
            .replace("__MINUTES__", f"{minutes:.1f}")
            .replace("__TARGET__", str(int(target)))
            .replace("__LOW__", str(int(target * 0.8)))
            .replace("__HIGH__", str(int(target * 1.2)))
            .replace("__TRANSCRIPT__", transcript))


def usable_segment_count(workdir):
    """Transcript segments carrying real text — the phase-1
    edge rule counts these: fewer than MIN_SEGMENTS means
    the video has (almost) no speech to commentate on, and
    v1 honestly refuses instead of inventing a commentary
    from nothing."""
    segs = load_segments(Path(workdir) / "segments_en.json")
    return sum(1 for s in segs if (s.get("text") or "").strip())


def generate_script(workdir, target_chars):
    """Walk the gateways for a commentary script of
    ~target_chars. Returns (script, provider_label). Raises
    RuntimeError with the polite Burmese message when every
    gateway fails — the caller fails the job (never
    charged)."""
    transcript = recap._transcript_text(workdir)
    if not transcript:
        raise RuntimeError(SPARSE_MSG)
    minutes = target_chars / CHARS_PER_MIN
    prompt = _prompt(transcript, minutes, target_chars)

    def extract(raw):
        out = recap._clean(raw)
        return out if recap._usable(out, target_chars) else None

    try:
        script, provider = story._chain_generate(prompt, extract)
    except RuntimeError as e:
        raise RuntimeError(FAIL_MSG + f" [{str(e)[:300]}]")
    return script, provider


def split_script(text, limit=450):
    """TTS chunks for the narration — recap's splitter,
    shared (sentence boundaries first, greedy packing)."""
    return recap.split_script(text, limit)


def script_segments(text, total_dur):
    """Timed segments for the render — recap's proportional
    layout, shared: chunks spread over the video's full
    duration by character share; the shared retime pass then
    fits the real speech into these windows."""
    return recap.script_segments(text, total_dur)


def target_chars_for(workdir):
    """The script size target for the job's source video:
    ~700 chars per real minute of video (recap's formula,
    shared)."""
    return recap.target_chars_for(workdir)


# ---------- the ducked original-audio bed ----------

def _atempo_chain(factor):
    """An atempo filter chain for an arbitrary factor:
    atempo accepts 0.5..100 per instance in modern ffmpeg,
    but chaining 0.5/2.0 steps keeps every version happy
    (the same approach as the studio's speed chain)."""
    parts = []
    f = float(factor)
    while f > 2.0:
        parts.append("atempo=2.0")
        f /= 2.0
    while f < 0.5:
        parts.append("atempo=0.5")
        f /= 0.5
    parts.append(f"atempo={f:.4f}")
    return ",".join(parts)


def _has_audio(path):
    try:
        return any(s.get("codec_type") == "audio"
                   for s in probe_streams(path))
    except Exception:
        return False


def mix_bed(workdir, volume=BED_VOLUME):
    """Mix the source's own audio, ducked to `volume`, under
    the finished narration. Returns True when the bed was
    mixed in, False when there was nothing to mix (no plan,
    no source audio, no narration track) — the caller treats
    False as "narration-only output stands", never as an
    error. Runs entirely from retime's persisted chunk plan,
    so the bed follows the SAME re-timing the picture got:
    each source chunk is stretched/compressed to its chunk's
    new duration before the concat."""
    workdir = Path(workdir)
    src = workdir / "source.mp4"
    out = workdir / "output.mp4"
    dub = workdir / "dub.wav"
    plan_path = workdir / "retime_plan.json"
    if not (src.exists() and out.exists() and dub.exists()
            and plan_path.exists()):
        return False
    if not _has_audio(src):
        return False
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        chunks = plan.get("chunks") or []
    except Exception:
        return False
    if not chunks:
        return False
    cdir = workdir / "bed_chunks"
    cdir.mkdir(exist_ok=True)
    wavs = []
    for n, c in enumerate(chunks):
        start = float(c.get("start") or 0)
        end = float(c.get("end") or 0)
        new_dur = float(c.get("new_dur") or 0)
        orig = end - start
        if orig <= 0 or new_dur <= 0:
            continue
        w = cdir / f"b{n:03d}.wav"
        af = (f"{_atempo_chain(orig / new_dur)},apad,"
              f"atrim=0:{new_dur:.3f}")
        try:
            run(["ffmpeg", "-y", "-v", "error",
                 "-ss", f"{start:.3f}", "-i", str(src),
                 "-t", f"{orig:.3f}", "-vn", "-af", af,
                 "-ac", "2", "-ar", "44100", str(w)],
                timeout=300)
        except Exception:
            # A chunk that will not decode becomes silence of
            # the chunk's length — the bed's timeline must
            # stay exact even where the source audio is odd.
            run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                 "-i", "anullsrc=channel_layout=stereo:"
                 "sample_rate=44100",
                 "-t", f"{new_dur:.3f}",
                 "-ac", "2", "-ar", "44100", str(w)],
                timeout=300)
        wavs.append(w)
    if not wavs:
        return False
    lst = cdir / "blist.txt"
    lst.write_text("".join(f"file '{f.resolve()}'\n" for f in wavs),
                   encoding="utf-8")
    bed = workdir / "bed.wav"
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat",
         "-safe", "0", "-i", str(lst), "-c:a", "pcm_s16le",
         str(bed)], timeout=1200)
    mixed = workdir / "mixed.wav"
    run(["ffmpeg", "-y", "-v", "error", "-i", str(dub),
         "-i", str(bed),
         "-filter_complex",
         f"[1:a]volume={volume:.3f}[bed];"
         "[0:a][bed]amix=inputs=2:duration=first:"
         "normalize=0[mix]",
         "-map", "[mix]", "-ac", "2", "-ar", "44100",
         str(mixed)], timeout=1200)
    final = workdir / "output_bed.mp4"
    run(["ffmpeg", "-y", "-v", "error", "-i", str(out),
         "-i", str(mixed), "-map", "0:v:0", "-map", "1:a:0",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "96k",
         "-shortest", str(final)], timeout=1200)
    final.replace(out)
    return True
