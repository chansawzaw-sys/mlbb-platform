"""Step 3: translate each segment to Myanmar (Burmese).

Provider chain:
  0. Gemini Flash (only when GEMINI_API_KEY is configured server-side —
     see pipeline/gemini_translate.py): ONE request for ALL segments,
     natural spoken Burmese. Any failure -> fall through to the free chain.
  1. deep-translator GoogleTranslator (unofficial Google Translate endpoint)
  2. `translators` library -> Microsoft Bing Translator web endpoint
  3. keep the source text (never drop a cue)

Why a chain: from this sandbox's shared egress IP, Google Translate answers
with its "sorry / too many requests" page (verified 2026-11-07) and MyMemory's
free daily quota for the IP was already exhausted, while Bing worked and
returned correct Burmese. On a normal residential IP, provider 1 usually works.

CLI: python -m pipeline.translate_segments <workdir>
Reads segments_en.json, writes segments_my.json + subs_my.srt (original timings).
"""
import re
import sys
import time
from pathlib import Path

from .common import ensure_system_ca, load_segments, save_segments, write_srt


def _with_timeout(fn, text, timeout=45):
    """Run a provider call under a hard ceiling.

    deep-translator and the translators library issue their HTTP requests
    with NO timeout, so one hung connection used to stall the whole job
    forever (the job sat at 'running' and blocked every new job behind
    the one-job gate). The worker thread is abandoned as a daemon and
    the call simply counts as a provider failure."""
    from concurrent.futures import ThreadPoolExecutor
    ex = ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(fn, text).result(timeout=timeout)
    finally:
        ex.shutdown(wait=False)


def _google(text):
    from deep_translator import GoogleTranslator
    return GoogleTranslator(source="auto", target="my").translate(text)


def _bing(text):
    import translators as ts
    return ts.translate_text(text, translator="bing",
                             from_language="en", to_language="my")


_PROVIDERS = [("google", _google), ("bing", _bing)]

# --- Why this step used to crawl -------------------------------------------
# The old loop called translate_one() per segment, and translate_one
# retried EVERY provider twice (with sleeps) for EVERY segment — from a
# datacenter IP where the free endpoints mostly fail, that is up to
# ~15-20 s of failing per segment, i.e. many minutes for a short clip.
# Now: (1) one BATCHED Google request per chunk of segments (a whole
# 90-second clip is usually a single request), with an exact-count
# alignment guard; (2) whatever the batch cannot do falls to a
# per-segment chain that REMEMBERS which provider works and which are
# dead, so failures are discovered once, not once per segment.


def _batch_google(texts):
    """Translate many segments in ONE Google request.

    Segments carry numbered markers ([[0]], [[1]], ...); the reply is
    split back on those markers. Raises unless every marker survives,
    so a mangled reply can never misalign translations."""
    joined = "\n".join(f"[[{i}]] {t}" for i, t in enumerate(texts))
    out = _with_timeout(_google, joined, timeout=60) or ""
    parts = {}
    cur = None
    for line in out.splitlines():
        m = re.match(r"\[\[\s*(\d+)\s*\]\]\s*(.*)", line)
        if m:
            cur = int(m.group(1))
            parts[cur] = m.group(2)
        elif cur is not None and line.strip():
            parts[cur] = (parts[cur] + " " + line.strip()).strip()
    if len(parts) != len(texts) or any(not parts.get(i, "").strip()
                                       for i in range(len(texts))):
        raise RuntimeError("batch reply misaligned")
    return [parts[i].strip() for i in range(len(texts))]


def _chunks(texts, limit=4000):
    """Group segment indices so each joined batch stays under the
    Google free endpoint's ~5000-char request limit."""
    groups, cur, size = [], [], 0
    for i, t in enumerate(texts):
        need = len(t) + 8
        if cur and size + need > limit:
            groups.append(cur)
            cur, size = [], 0
        cur.append(i)
        size += need
    if cur:
        groups.append(cur)
    return groups


class _Chain:
    """Per-segment fallback chain with a memory: a provider that works
    is tried first; one that fails twice is skipped for the rest of
    the job; once a good provider is known there are no retry sleeps."""

    def __init__(self):
        self.good = None
        self.fails = {}

    def one(self, text):
        order = sorted(_PROVIDERS, key=lambda p: p[0] != self.good)
        last_err = None
        for name, fn in order:
            if self.fails.get(name, 0) >= 2:
                continue
            attempts = 1 if self.good else 2
            for attempt in range(attempts):
                try:
                    out = (_with_timeout(fn, text) or "").strip()
                    if out:
                        self.good = name
                        return out, name
                except Exception as e:
                    last_err = e
                    self.fails[name] = self.fails.get(name, 0) + 1
                    if attempt + 1 < attempts:
                        time.sleep(1.0)
        return text, f"fallback-source ({last_err})"


def translate_one(text):
    """Return (burmese_text, provider_name) — single-shot, no memory."""
    return _Chain().one(text)


def translate_segments(workdir):
    ensure_system_ca()
    workdir = Path(workdir)
    segs = load_segments(workdir / "segments_en.json")
    # 0. Gemini batch — first choice when a key is configured.
    if segs:
        try:
            from . import gemini_translate
            if gemini_translate.key_configured():
                by_idx = gemini_translate.translate_all(segs)
                out = [{**seg, "text_my": by_idx[i], "provider": "gemini"}
                       for i, seg in enumerate(segs)]
                save_segments(out, workdir / "segments_my.json")
                write_srt(out, workdir / "subs_my.srt")
                return out
        except Exception:
            pass  # any Gemini failure -> free chain below, as designed
    texts = [(seg.get("text") or "").strip() for seg in segs]
    results = [None] * len(segs)
    # 1. Batched Google — one request per chunk.
    for group in _chunks(texts):
        if all(not texts[i] for i in group):
            for i in group:
                results[i] = ("", "empty")
            continue
        try:
            got = _batch_google([texts[i] for i in group])
            for i, t in zip(group, got):
                results[i] = (t, "google-batch")
        except Exception:
            pass  # the chain below handles this chunk segment by segment
    # 2. Per-segment chain (with memory) for whatever is left.
    chain = _Chain()
    for i, seg in enumerate(segs):
        if results[i] is None:
            if not texts[i]:
                results[i] = ("", "empty")
            else:
                results[i] = chain.one(texts[i])
    out = [{**seg, "text_my": results[i][0], "provider": results[i][1]}
           for i, seg in enumerate(segs)]
    save_segments(out, workdir / "segments_my.json")
    write_srt(out, workdir / "subs_my.srt")
    return out


if __name__ == "__main__":
    res = translate_segments(Path(sys.argv[1]))
    provs = {}
    for r in res:
        provs[r["provider"]] = provs.get(r["provider"], 0) + 1
    print(f"{len(res)} segments translated, providers: {provs}")
