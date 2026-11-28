"""Word-corrected subtitle cue timings — build 271.

The owner's standing rule (2026-11-22): translated subtitle
text must appear ONLY while the words are actually being
spoken — never show the next line early during a silence.
The pipeline used to trust the Whisper segment timestamps
for every display timing, but a segment's window routinely
starts before its first word (and ends after its last), so
a cue could sit on screen through a pause that precedes
its speech.

The transcription stage already produces WORD timestamps
for Groq jobs (pipeline/groq_transcribe.py via
pipeline/transcribe.py -> <workdir>/words.json:
[{start, end, word}]); the local faster-whisper fallback
yields none today (it does not request word timestamps),
so local-fallback and captions-sourced jobs have no word
data and keep their segment timing unchanged — this module
is a strict no-op for them.

What is built here, ONCE per job, from segments_my.json +
words.json:

  cues — [{start, end, text_my, seg}] in SOURCE time, where
  each segment's display window is re-aligned to its spoken
  words (start = first word's start, end = last word's
  end), and a segment is SPLIT at every silence of
  SILENCE_CUT_SECONDS or more between consecutive words
  (no cue is ever on screen during such a gap; a shorter
  pause never splits). The split of the TRANSLATED text
  follows the word boundary: parts are proportional to
  the source-word counts they cover, snapped to spaces,
  and a cue is never empty (word groups merge until the
  text can be shared out non-emptily).

The cue list is persisted as <workdir>/cues_my.json by
write_subs_srt — the single writer every subs_my.srt in
the dub/subs/captions flows now goes through — and every
consumer reads from it:

  * subtitle burn-in: subs mode burns subs_my.srt (written
    from the cues); both mode burns subs_retimed.srt
    (retime maps the SAME cues through its plan);
  * the .srt downloads serve subs_my.srt, and the
    /export/srt route emits the validated cues directly;
  * karaoke is a burn style over those same SRT files;
  * dub placement: retime takes its segment windows from
    the cues (see segment_windows), so a segment's dub
    starts at its first spoken word, not at the Whisper
    segment's early start.

Recap/Commentary script jobs never write cues_my.json
(their segments_my.json is rebuilt from a script with
proportional timings, not from the transcript), so retime
treats them exactly as before — the cue file's PRESENCE
is the gate, and retime additionally validates the cues
against the current segments (validated_cues) before
trusting them.
"""
from pathlib import Path

from .common import load_segments, save_segments, write_srt

# A pause between consecutive spoken words at or above this
# length cuts the cue: nothing is displayed across it.
SILENCE_CUT_SECONDS = 0.5

CUES_NAME = "cues_my.json"
WORDS_NAME = "words.json"


def load_words(workdir):
    """The job's word timestamps [{start, end, word}] from
    words.json, floats coerced and sorted by start — or
    None when the job has no usable word data (missing /
    unparseable / empty file, or no valid entries)."""
    try:
        raw = load_segments(Path(workdir) / WORDS_NAME)
    except Exception:
        return None
    if not isinstance(raw, list):
        return None
    words = []
    for w in raw:
        if not isinstance(w, dict):
            continue
        try:
            start = float(w["start"])
            end = float(w["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end < start:
            continue
        words.append({"start": start, "end": end,
                      "word": str(w.get("word") or "")})
    if not words:
        return None
    words.sort(key=lambda w: (w["start"], w["end"]))
    return words


def assign_words(segs, words):
    """Words per segment, by maximum time overlap: a word
    belongs to the segment its [start, end] overlaps most
    (Whisper segments are built from the words, so in
    practice every word lands wholly inside its own
    segment). A word overlapping no segment — it sits in a
    gap the segmenter skipped — belongs to no cue and is
    dropped. Returns a list parallel to `segs`."""
    out = [[] for _ in segs]
    bounds = []
    for seg in segs:
        try:
            bounds.append((float(seg["start"]), float(seg["end"])))
        except (KeyError, TypeError, ValueError):
            bounds.append((0.0, 0.0))
    si = 0
    for w in words:
        ws, we = float(w["start"]), float(w["end"])
        while si < len(bounds) and bounds[si][1] <= ws:
            si += 1
        best, best_ov = None, 0.0
        j = si
        while j < len(bounds) and bounds[j][0] < we:
            ov = min(we, bounds[j][1]) - max(ws, bounds[j][0])
            if ov > best_ov:
                best, best_ov = j, ov
            j += 1
        if best is not None:
            out[best].append(w)
    return out


def _snap_space(text, target, lo, hi):
    """The split position nearest `target` within [lo, hi]
    that sits next to a space (so a split cue never starts
    or ends mid-word when the text has spaces to snap to);
    the clamped target itself when no space is in range."""
    best = None
    for i in range(lo, hi + 1):
        if 0 < i < len(text) and (text[i - 1] == " "
                                  or text[i] == " "):
            if best is None or abs(i - target) < abs(best - target):
                best = i
    return best if best is not None else min(max(target, lo), hi)


def split_text(text, weights):
    """Share `text` over len(weights) parts, proportionally
    to the weights (source-word counts), boundaries snapped
    to spaces. Returns the parts (each stripped, in order),
    or None when the text cannot fill that many non-empty
    parts — the caller then merges word groups and retries.
    The parts always partition the text: joining them
    reproduces it up to whitespace."""
    n = len(weights)
    if n <= 1:
        return [text.strip()] if text.strip() else None
    if not text.strip():
        return None
    total = float(sum(weights)) or float(n)
    bounds, acc = [], 0.0
    for w in weights[:-1]:
        acc += w
        bounds.append(len(text) * acc / total)
    idx, prev = [], 0
    for k, b in enumerate(bounds):
        lo = prev + 1
        hi = len(text) - (n - 1 - k)
        if hi < lo:
            return None
        j = _snap_space(text, int(round(b)), lo, hi)
        idx.append(j)
        prev = j
    parts, start = [], 0
    for j in idx:
        parts.append(text[start:j])
        start = j
    parts.append(text[start:])
    parts = [p.strip() for p in parts]
    if any(not p for p in parts):
        return None
    return parts


def segment_cues(seg, seg_words):
    """The display cues for ONE translated segment:
    [{start, end, text_my}] in source time.

    No words (the segmenter heard speech the word stream
    does not cover, or the job has no word data at all):
    the segment keeps its own window and its full text —
    build 270's behaviour for that segment. With words:
    the window re-aligns to the first/last spoken word and
    splits at every >= SILENCE_CUT_SECONDS pause between
    consecutive words, the translated text shared over the
    resulting word groups by word count. A segment with no
    translated text produces no cues (write_srt has always
    skipped those)."""
    text = (seg.get("text_my") or "").strip()
    if not text:
        return []
    try:
        start, end = float(seg["start"]), float(seg["end"])
    except (KeyError, TypeError, ValueError):
        return []
    if not seg_words:
        return [{"start": round(start, 3), "end": round(end, 3),
                 "text_my": text}]
    groups = [[seg_words[0]]]
    for prev_w, cur_w in zip(seg_words, seg_words[1:]):
        if float(cur_w["start"]) - float(prev_w["end"]) \
                >= SILENCE_CUT_SECONDS:
            groups.append([cur_w])
        else:
            groups[-1].append(cur_w)
    parts = None
    while groups:
        parts = split_text(text, [len(g) for g in groups])
        if parts is not None or len(groups) == 1:
            break
        # The text cannot fill this many cues non-emptily:
        # merge the last word group into its neighbour (the
        # merged cue spans that pause — an empty cue would
        # be worse) and share the text out again.
        groups[-2].extend(groups[-1])
        groups.pop()
    if parts is None:
        parts = [text]
    cues = []
    for group, part in zip(groups, parts):
        cs = max(0.0, float(group[0]["start"]))
        ce = float(group[-1]["end"])
        if ce <= cs:
            ce = cs + 0.1
        cues.append({"start": round(cs, 3), "end": round(ce, 3),
                     "text_my": part})
    return cues


def build_cues(my_segs, words):
    """The job's corrected cue list from its translated
    segments + word timestamps, or None when there is no
    word data (the caller then keeps segment timing
    everywhere). Each cue carries its segment's index
    under "seg" so consumers can regroup per segment."""
    if not words:
        return None
    assigned = assign_words(my_segs, words)
    cues = []
    for i, seg in enumerate(my_segs):
        for cue in segment_cues(seg, assigned[i]):
            cue["seg"] = i
            cues.append(cue)
    return cues


def cues_for_workdir(workdir, my_segs=None):
    """build_cues for a job workdir (words.json + the given
    segments, or segments_my.json read from disk), with the
    result persisted to cues_my.json — the artifact every
    consumer gates on. None when the job has no word data.
    Persisting is best-effort: the returned cues are valid
    either way, and a failed write only means retime falls
    back to segment timing for this job."""
    words = load_words(workdir)
    if not words:
        return None
    if my_segs is None:
        try:
            my_segs = load_segments(Path(workdir) / "segments_my.json")
        except Exception:
            return None
    cues = build_cues(my_segs, words)
    if cues is None:
        return None
    try:
        save_segments(cues, Path(workdir) / CUES_NAME)
    except Exception:
        pass
    return cues


def write_subs_srt(workdir, segs, out_path):
    """The one writer for subs_my.srt in the dub/subs/
    captions flows (translate, review edits, captions
    workers, bulk assign): the SRT is written from the
    word-corrected cues when the job has word data, and
    from the segments themselves — byte-identical to the
    pre-271 write_srt call — when it does not."""
    cues = cues_for_workdir(workdir, segs)
    if cues is None:
        return write_srt(segs, out_path)
    return write_srt(cues, out_path)


def load_cues(workdir):
    """The persisted cues_my.json, shape-checked, or None."""
    try:
        cues = load_segments(Path(workdir) / CUES_NAME)
    except Exception:
        return None
    if not isinstance(cues, list) or not cues:
        return None
    for c in cues:
        if not isinstance(c, dict) \
                or not isinstance(c.get("seg"), int) \
                or "start" not in c or "end" not in c:
            return None
    return cues


def _squash(text):
    return "".join((text or "").split())


def validated_cues(workdir, segs):
    """The job's persisted cues, trusted only when they
    still describe THESE segments: every cue's segment
    index is in range and each segment's cue texts join
    (whitespace-insensitively) to that segment's current
    text_my. Any mismatch — the segments were rebuilt or
    edited through a path that did not refresh the cues —
    discards the whole list rather than misapplying half
    of it. None when there are no cues or they are stale."""
    cues = load_cues(workdir)
    if not cues:
        return None
    by_seg = {}
    for c in cues:
        i = c["seg"]
        if i < 0 or i >= len(segs):
            return None
        by_seg.setdefault(i, []).append(c)
    for i, group in by_seg.items():
        joined = "".join(c.get("text_my") or "" for c in group)
        if _squash(joined) != _squash(segs[i].get("text_my")):
            return None
    return cues


def segment_windows(cues):
    """Per-segment corrected windows from a cue list:
    {seg_index: (first cue start, last cue end)} — retime's
    plan reads its segment windows from here, so dub
    placement starts each segment at its first spoken
    word. Segments without cues are absent (their own
    window stands)."""
    wins = {}
    for c in cues:
        i = c["seg"]
        if i in wins:
            wins[i] = (min(wins[i][0], float(c["start"])),
                       max(wins[i][1], float(c["end"])))
        else:
            wins[i] = (float(c["start"]), float(c["end"]))
    return wins
