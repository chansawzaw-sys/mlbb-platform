"""Step 3: translate each segment to Myanmar (Burmese).

Provider chain:
  0. Gemini Flash (only when GEMINI_API_KEY is configured server-side —
     see pipeline/gemini_translate.py): ONE request for ALL segments,
     natural spoken Burmese. Any failure -> OpenAI (build 134 module,
     when configured) -> Apps Script (build 137 module, the owner's
     own Google account via LanguageApp, when configured), then the
     free chain below. (Order set by the owner 2026-11-11, build 136:
     Gemini -> OpenAI -> free; Apps Script added after OpenAI by the
     same order — reliable before throttled, style after Gemini/OpenAI.)
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
import random
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


def _google_gtx(text):
    """Raw translate_a/single (client=gtx) call — a different request
    shape, and in practice a different rate-limit bucket, than
    deep-translator's path (the same reason the tw-ob TTS endpoint
    answered while the batchexecute endpoint was 429ing)."""
    import requests
    params = {"client": "gtx", "sl": "auto", "tl": "my", "dt": "t",
              "q": text}
    url = "https://translate.googleapis.com/translate_a/single"
    headers = {"User-Agent": "Mozilla/5.0"}
    if len(text) < 1500:
        r = requests.get(url, params=params, headers=headers, timeout=30)
    else:
        r = requests.post(url, data=params, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()
    out = "".join(part[0] for part in (data[0] or []) if part and part[0])
    if not out.strip():
        raise RuntimeError("gtx returned empty")
    return out


def _mymemory(text):
    """MyMemory free tier — a small per-IP daily quota, so it sits late
    in the chain as a rescue for individual segments, never batches."""
    import requests
    r = requests.get("https://api.mymemory.translated.net/get",
                     params={"q": text, "langpair": "en|my"},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    data = r.json()
    out = ((data.get("responseData") or {}).get("translatedText") or "").strip()
    if not out or "YOU USED ALL AVAILABLE" in out.upper() \
            or str(data.get("responseStatus")) not in ("200", "None"):
        raise RuntimeError(f"mymemory: {data.get('responseStatus')}")
    return out


_PROVIDERS = [("google", _google), ("gtx", _google_gtx),
              ("bing", _bing), ("mymemory", _mymemory)]

# The provider that worked is remembered ACROSS jobs (a tiny file in
# the app dir), so every new job starts with the known-good one first
# instead of re-discovering the failures each time.
_GOOD_FILE = Path(__file__).resolve().parent.parent / ".translate_good"


def _load_good():
    try:
        name = _GOOD_FILE.read_text(encoding="utf-8").strip()
        return name if any(name == n for n, _ in _PROVIDERS) else None
    except Exception:
        return None


def _save_good(name):
    try:
        _GOOD_FILE.write_text(name, encoding="utf-8")
    except Exception:
        pass

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
    return _split_marked(out, len(texts))


def _split_marked(out, count):
    parts = {}
    cur = None
    for line in (out or "").splitlines():
        m = re.match(r"\[\[\s*(\d+)\s*\]\]\s*(.*)", line)
        if m:
            cur = int(m.group(1))
            parts[cur] = m.group(2)
        elif cur is not None and line.strip():
            parts[cur] = (parts[cur] + " " + line.strip()).strip()
    if len(parts) != count or any(not parts.get(i, "").strip()
                                  for i in range(count)):
        raise RuntimeError("batch reply misaligned")
    return [parts[i].strip() for i in range(count)]


def _batch_gtx(texts):
    """The batch trick through the gtx endpoint (see _google_gtx)."""
    joined = "\n".join(f"[[{i}]] {t}" for i, t in enumerate(texts))
    out = _with_timeout(_google_gtx, joined, timeout=60) or ""
    return _split_marked(out, len(texts))


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


def _is_throttle(err):
    s = str(err).lower()
    return "429" in s or "too many" in s or "rate" in s \
        or "quota" in s or "403" in s


_PACE_S = 1.1        # min seconds between two requests to one provider
_PACE_JITTER = 0.4   # plus a small random spread, so the requests do
                     # not march in a perfectly regular robot rhythm


class _Chain:
    """Per-segment fallback chain with a memory: a provider that works
    is tried first; one that fails twice is skipped for the rest of
    the job; once a good provider is known there are no retry sleeps.

    D1 (2026-11-10): a provider that answers with a rate-limit style
    error goes into a 30 s cooldown — it is tried LAST (and only if
    nothing else worked) instead of being hammered once per segment
    while the endpoint is throttling the server IP, which is how a
    whole job's chain collapsed in ~2 minutes on 2026-11-09 while the
    single-sentence probe still passed. Every call's outcome is also
    recorded in self.report for the job's translate_report.json.

    D2 (2026-11-10): requests to the same provider are PACED — at
    least _PACE_S seconds apart, plus jitter. The 21:19 job's report
    showed the whole Google family hard-throttled from the server IP
    while Bing carried 24 segments and then the burst tripped its
    limit too; slow, steady requests keep the one working carrier
    alive for the whole job instead of burning it in the first
    minute."""

    def __init__(self):
        self.good = _load_good()
        self.fails = {}
        self.cooldown = {}
        self.report = {}
        self._slept_at = 0.0
        self._last = {}
        self.paced = 0.0

    def _pace(self, name):
        now = time.time()
        last = self._last.get(name)
        if last is not None:
            wait = _PACE_S - (now - last) + random.uniform(0, _PACE_JITTER)
            if wait > 0:
                time.sleep(wait)
                self.paced += wait
        self._last[name] = time.time()

    def _note(self, name, ok, err=None):
        r = self.report.setdefault(name, {"ok": 0, "fail": 0, "last": ""})
        if ok:
            r["ok"] += 1
        else:
            r["fail"] += 1
            if err is not None:
                r["last"] = str(err)[:80]

    def one(self, text):
        order = sorted(_PROVIDERS, key=lambda p: p[0] != self.good)
        now = time.time()
        ready = [p for p in order if self.cooldown.get(p[0], 0) <= now]
        cooling = [p for p in order if self.cooldown.get(p[0], 0) > now]
        last_err = None
        for name, fn in ready + cooling:
            if self.fails.get(name, 0) >= 2:
                continue
            self._pace(name)
            attempts = 1 if self.good else 2
            for attempt in range(attempts):
                try:
                    out = (_with_timeout(fn, text) or "").strip()
                    if out and out == text.strip() \
                            and any(c.isalpha() for c in text) \
                            and not any("\u1000" <= c <= "\u109f"
                                        for c in out):
                        # The provider ECHOED the source (MyMemory
                        # does this for zh->my) — that is not a
                        # translation; count it as a failure and
                        # move on to the next provider.
                        self.fails[name] = self.fails.get(name, 0) + 1
                        last_err = RuntimeError(f"{name} echoed source")
                        self._note(name, False, last_err)
                        continue
                    if out:
                        self.good = name
                        _save_good(name)
                        self._note(name, True)
                        return out, name
                    self._note(name, False, "empty reply")
                except Exception as e:
                    last_err = e
                    self.fails[name] = self.fails.get(name, 0) + 1
                    self._note(name, False, e)
                    if _is_throttle(e):
                        self.cooldown[name] = time.time() + 30
                        # One short pause per burst window lets the
                        # endpoint's per-IP pressure drop instead of
                        # every segment re-tripping the limit.
                        if time.time() - self._slept_at > 10:
                            self._slept_at = time.time()
                            time.sleep(2.0)
                        break
                    if attempt + 1 < attempts:
                        time.sleep(1.0)
        return text, f"fallback-source ({last_err})"


def translate_one(text):
    """Return (burmese_text, provider_name) — single-shot, no memory."""
    return _Chain().one(text)


def _fill_missing(out, report=None):
    """No silent gaps (owner complaint 2026-11-09: subtitle lines
    missing from the finished video). Batch endpoints — Gemini
    included — sometimes return an EMPTY string for a segment, and
    write_srt/TTS then skip it, leaving a hole. Retry each empty
    segment once per-segment; the chain's own last resort is the
    source text, so a line is never dropped silently.

    D1 (2026-11-10): ECHOED segments (batch returned the source text
    unchanged) are rescued the same way — a minority of echoes used
    to ship silently as foreign-language subtitles because only the
    empty case was retried.

    D2 (2026-11-10): fallback-source segments (the main chain gave
    up on them) are rescued too — earlier they were skipped here,
    which made every fill pass blind to exactly the segments a
    throttled job produces most. A fresh chain has fresh fail
    counters, and by fill time (let alone the second wind's) the
    provider may well have recovered."""
    chain = None
    for seg in out:
        if not (seg.get("text") or "").strip():
            continue
        empty = not (seg.get("text_my") or "").strip()
        echoed = not empty and _seg_untranslated(seg) \
            and not (seg.get("provider") or "").startswith("fallback-source")
        failed = (seg.get("provider") or "").startswith("fallback-source")
        if empty or echoed or failed:
            if chain is None:
                chain = _Chain()
            new_my, new_prov = chain.one(seg["text"].strip())
            # Keep whichever result is actually translated.
            if not _seg_untranslated({**seg, "text_my": new_my,
                                       "provider": new_prov}):
                seg["text_my"], seg["provider"] = new_my, new_prov
            elif empty:
                seg["text_my"], seg["provider"] = new_my, new_prov
    if report is not None and chain is not None:
        report["fill"] = chain.report
        report["fill_paced_s"] = round(chain.paced, 1)
    return out


def _seg_untranslated(seg):
    """True when a segment effectively stayed in the source
    language: the chain's last-resort fallback fired, or the
    'translation' is byte-identical to a non-Myanmar source.
    (Owner report 2026-11-09: a finished job whose TXT export was
    entirely Chinese — every provider had failed and the source
    text silently stood in as the 'translation'.)"""
    src = (seg.get("text") or "").strip()
    my = (seg.get("text_my") or "").strip()
    if not src or not my:
        return False
    if any("\u1000" <= ch <= "\u109f" for ch in src):
        return False  # source already Myanmar: identical is fine
    if (seg.get("provider") or "").startswith("fallback-source"):
        return True
    # Identical output counts as untranslated when the source has
    # any letters (CJK included) — short drama lines are often 2-3
    # chars, while pure numbers/names legitimately stay unchanged.
    return my == src and any(ch.isalpha() for ch in src)


def translation_stats(out):
    total = sum(1 for s in out if (s.get("text") or "").strip())
    un = sum(1 for s in out if _seg_untranslated(s))
    return {"total": total, "untranslated": un}


def _usable(src, out):
    """A batch item counts only if it is non-empty and not a source
    echo (D1: batch replies used to be accepted wholesale once the
    marker alignment matched, so echoed segments shipped as foreign
    subtitles and a poisoned chunk hid behind a good provider name)."""
    o = (out or "").strip()
    if not o:
        return False
    return not _seg_untranslated(
        {"text": src, "text_my": o, "provider": "batch"})


def _write_report(workdir, report):
    try:
        import json
        (workdir / "translate_report.json").write_text(
            json.dumps(report, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def translate_segments(workdir, style="subs"):
    ensure_system_ca()
    workdir = Path(workdir)
    segs = load_segments(workdir / "segments_en.json")
    report = {"batch": {}, "chain": {}, "fill": {}}
    # 0. Gemini batch — the PRIMARY engine since build 136 (owner's
    # order 2026-11-11: fresh Gemini key first, OpenAI second, the
    # free chain last). Any failure falls through to OpenAI, with the
    # error kept in the report for diagnosis.
    if segs:
        try:
            from . import gemini_translate
            if gemini_translate.key_configured():
                by_idx = gemini_translate.translate_all(segs, style=style)
                out = [{**seg, "text_my": by_idx[i], "provider": "gemini"}
                       for i, seg in enumerate(segs)]
                out = _fill_missing(out, report)
                _write_report(workdir, report)
                save_segments(out, workdir / "segments_my.json")
                write_srt(out, workdir / "subs_my.srt")
                return out
        except Exception as e:
            report["gemini_err"] = str(e)[:120]
    # 0b. OpenAI batch — second engine, when a key is configured.
    if segs:
        try:
            from . import openai_translate
            if openai_translate.key_configured():
                by_idx = openai_translate.translate_all(segs, style=style)
                out = [{**seg, "text_my": by_idx[i], "provider": "openai"}
                       for i, seg in enumerate(segs)]
                out = _fill_missing(out, report)
                _write_report(workdir, report)
                save_segments(out, workdir / "segments_my.json")
                write_srt(out, workdir / "subs_my.srt")
                return out
        except Exception as e:
            report["openai_err"] = str(e)[:120]
    # 0c. OpenRouter batch — third engine (build 147, owner chose it
    # 2026-11-11): direct OpenAI is region-blocked from the VPS, so
    # the same wallet idea runs through OpenRouter (US aggregator,
    # Gemini models first, story-style prompts apply). Any failure
    # falls through to Apps Script, then the free chain.
    if segs:
        try:
            from . import openrouter_translate
            if openrouter_translate.key_configured():
                by_idx = openrouter_translate.translate_all(
                    segs, style=style)
                out = [{**seg, "text_my": by_idx[i],
                        "provider": "openrouter"}
                       for i, seg in enumerate(segs)]
                out = _fill_missing(out, report)
                _write_report(workdir, report)
                save_segments(out, workdir / "segments_my.json")
                write_srt(out, workdir / "subs_my.srt")
                return out
        except Exception as e:
            report["openrouter_err"] = str(e)[:120]
    # 0d. Apps Script batch — fourth engine (build 137, owner request
    # 2026-11-10): the owner's own Google account translates via
    # LanguageApp, so the throttled VPS IP never touches Google.
    # Reliable fallback before the free chain; literal Google quality,
    # no narration style. Any failure falls through to the free chain.
    if segs:
        try:
            from . import apps_script_translate
            if apps_script_translate.configured():
                by_idx = apps_script_translate.translate_all(
                    segs, style=style)
                out = [{**seg, "text_my": by_idx[i],
                        "provider": "apps-script"}
                       for i, seg in enumerate(segs)]
                out = _fill_missing(out, report)
                _write_report(workdir, report)
                save_segments(out, workdir / "segments_my.json")
                write_srt(out, workdir / "subs_my.srt")
                return out
        except Exception as e:
            report["apps_script_err"] = str(e)[:120]
    texts = [(seg.get("text") or "").strip() for seg in segs]
    results = [None] * len(segs)
    # 1. Batched free endpoints — one request per chunk, Google first,
    # then the gtx endpoint (a different bucket) for whatever is left.
    # Each returned item is validated on its own (non-empty, not an
    # echo); bad items fall to the per-segment chain below.
    for group in _chunks(texts):
        if all(not texts[i] for i in group):
            for i in group:
                results[i] = ("", "empty")
            continue
        got, prov = None, None
        for batch_fn, batch_name in ((_batch_google, "google-batch"),
                                     (_batch_gtx, "gtx-batch")):
            try:
                got, prov = batch_fn([texts[i] for i in group]), batch_name
                break
            except Exception as e:
                report["batch"].setdefault(
                    batch_name, {"ok": 0, "bad": 0, "err": ""})["err"] = \
                    str(e)[:80]
        if got:
            b = report["batch"].setdefault(
                prov, {"ok": 0, "bad": 0, "err": ""})
            for i, t in zip(group, got):
                if _usable(texts[i], t):
                    results[i] = (t, prov)
                    b["ok"] += 1
                else:
                    b["bad"] += 1
    # 2. Per-segment chain (with memory) for whatever is left.
    chain = _Chain()
    for i, seg in enumerate(segs):
        if results[i] is None:
            if not texts[i]:
                results[i] = ("", "empty")
            else:
                results[i] = chain.one(texts[i])
    report["chain"] = chain.report
    report["paced_s"] = round(chain.paced, 1)
    out = [{**seg, "text_my": results[i][0], "provider": results[i][1]}
           for i, seg in enumerate(segs)]
    out = _fill_missing(out, report)
    # D2 "second wind": the 21:19 job died with 24 of ~50 segments
    # translated — the working provider was throttled mid-job and the
    # first fill pass ran while it was still cooling down. When real
    # progress was made but a remainder is left, wait out the
    # throttle window ONCE and run one more fill pass with a fresh
    # chain before giving up. Costs under a minute; rescues exactly
    # the near-miss jobs that used to fail whole.
    stats = translation_stats(out)
    if stats["untranslated"] > 0 \
            and stats["total"] - stats["untranslated"] > 0:
        pause = 40.0 if stats["untranslated"] >= 5 else 6.0
        report["second_wind"] = {
            "remaining_before": stats["untranslated"], "pause_s": pause}
        time.sleep(pause)
        tmp = {}
        out = _fill_missing(out, tmp)
        report["fill2"] = tmp.get("fill", {})
        report["second_wind"]["remaining_after"] = \
            translation_stats(out)["untranslated"]
    _write_report(workdir, report)
    save_segments(out, workdir / "segments_my.json")
    write_srt(out, workdir / "subs_my.srt")
    return out


if __name__ == "__main__":
    res = translate_segments(Path(sys.argv[1]))
    provs = {}
    for r in res:
        provs[r["provider"]] = provs.get(r["provider"], 0) + 1
    print(f"{len(res)} segments translated, providers: {provs}")
