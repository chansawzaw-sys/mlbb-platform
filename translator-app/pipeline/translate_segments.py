"""Step 3: translate each segment to Myanmar (Burmese).

Provider walk — LLM engines only (build 231 removed the
literal free chain on the owner's order: its word-for-word
output was the quality floor he rejected, so a job whose LLM
engines all fail now fails LOUDLY with zero charge instead of
shipping source-language subtitles):
  0-lead. Gemini Flash FIRST, but only while the app probe's
     fresh verdict says it works (build 189): through the
     owner's VPN tunnel (pipeline/tunnel.py) the location
     block is gone, and Gemini is the quality target — free
     on the owner's key. A red or stale verdict removes this
     slot and the order below is exactly the pre-189 one.
  0. OpenRouter (build 147 module, when configured): ONE
     request for ALL segments, natural spoken Burmese. Any
     failure -> xKiro (build 155 module) -> Gemini Flash
     (build 136 module, when a key is configured) -> OpenAI
     (build 134 module, when configured) -> the per-segment
     LLM rescue (the _Rescue below) for whatever the batch
     slots could not carry.
     Build 168 (owner order 2026-11-13): FREE-FIRST — no
     paid credit purchase for now, so the best free models
     lead each gateway walk (ERNIE/Gemma on OpenRouter,
     Aya/Command A+/Mistral on xKiro), paid models sit at
     the back of their walks, and the region-blocked direct
     engines stop burning time ahead of the working
     gateways; the probe skip (build 186) removes a
     region-blocked engine while its verdict stays fresh.
  Build 232 (owner order 2026-11-20): ADMIN jobs walk a
     different order — Gemini FIRST (the probe's region-block
     skip still applies), OpenAI SECOND, then the order above
     as further fallback; the per-segment rescue follows the
     same preference. The owner runs his own jobs on the admin
     account and wants Gemini quality first on them, with his
     prepaid OpenAI balance as the second route. Non-admin
     jobs keep the order above byte for byte. The flag is the
     job account's role — the same lookup that exempts admin
     jobs from charging — threaded app.py -> run_phase1 ->
     here; every other caller keeps the default.
  Build 248 (owner complaint 2026-11-22/23: translation
     "makes no sense" on long videos, and 1-3 h jobs failed):
     the walk above used to receive ALL segments in ONE
     request — ~800-2,500 segments for a long video — and
     models truncated, lost the story thread, or the request
     failed outright, after which the guard failed the whole
     job. The segment list is now split into consecutive
     chunks of roughly 10 minutes of audio (segment end
     times; a 120-segment ceiling bounds the request when
     captions are dense, and timing-less segments fall back
     to the ceiling alone). Each chunk runs the SAME walk
     (the refactored _walk_batch below — same order, same
     probe rules, same admin variant), sequentially, and
     every chunk after the first receives a carry-over
     context: the story so far (the most recent translated
     lines, source -> Burmese) plus a glossary of the
     Latin-letter names established so far, threaded into
     the engines' shared prompt builder so names and story
     stay consistent across chunk boundaries. Segment count
     and order never change: each chunk's output is
     validated for exact count/order against its input
     before acceptance, a failed chunk falls back exactly
     as the pre-248 walk did (down the engine order, then
     the per-segment rescue), and the run_job guard still
     fails the job loudly when a majority stays
     untranslated. A job that fits in ONE chunk is the
     pre-248 behaviour byte for byte (context is None, the
     prompts are identical, the report shape is identical
     plus the new "chunks" list). translate_report.json
     records per chunk: index, segment range, the winning
     provider, and the chunk's own walk report (errors,
     fill, rescue, second wind) — so a bad job's report
     shows WHERE it degraded.

CLI: python -m pipeline.translate_segments <workdir>
Reads segments_en.json, writes segments_my.json + subs_my.srt (original timings).
"""
import json
import re
import sys
import time
from pathlib import Path

from .common import (PROJECT_ROOT, ensure_system_ca, load_segments,
                     save_segments, write_srt)


# --- The per-segment LLM rescue (build 231) -------------------
# The literal free chain that used to live here — direct web
# machine-translation endpoints, their batched requests, and
# the cross-job "which provider worked" memory file — is
# GONE, on the owner's order: its word-for-word output was
# the quality floor he rejected. What replaces it retries
# single segments through the SAME LLM walk the batch slots
# use, so every route a job can take is an LLM route, and a
# total failure surfaces as untranslated segments for
# run_job's guard to reject loudly (zero charge) instead of
# a finished video wearing source-language subtitles.


def _engine_translate(mod, segs, style, context):
    """One engine call (build 248): the chunk carry-over
    context is passed ONLY when there is one. A context-free
    call keeps the exact pre-248 call shape
    (translate_all(segs, style=style)), so single-chunk jobs
    — and anything written against the old signature — see
    no difference at all."""
    if context:
        return mod.translate_all(segs, style=style,
                                  context=context)
    return mod.translate_all(segs, style=style)


class _Rescue:
    """Per-segment rescue through the LLM engines, with the
    old chain's memory discipline: an engine that fails
    twice is skipped for the rest of the pass, and every
    call's outcome is recorded in self.report for the job's
    translate_report.json. Gemini's lead rules are
    preserved (configured + the probe's fresh region-block
    skip); OpenAI keeps the same skip. Build 232: an admin
    job's rescue follows the admin batch preference —
    Gemini, then OpenAI, then the remaining engines in
    their usual relative order. Build 248: a chunk's
    carry-over context (when the rescue runs for a later
    chunk) rides along on every per-segment call, so a
    rescued line still lands in the established names and
    story voice."""

    def __init__(self, style="subs", admin=False, context=None):
        self.style = style
        self.admin = admin
        self.context = context
        self.fails = {}
        self.report = {}

    def _note(self, name, ok, err=None):
        r = self.report.setdefault(
            name, {"ok": 0, "fail": 0, "last": ""})
        if ok:
            r["ok"] += 1
        else:
            r["fail"] += 1
            if err is not None:
                r["last"] = str(err)[:80]

    def _attempts(self):
        """(engine, fn) pairs in walk order; fn(seg) returns
        (text, provider_label). Unconfigured or
        probe-skipped engines are simply absent. Build 232:
        the availability probes below are order-free — the
        walk order is applied at the end, so an admin pass
        promotes OpenAI to second while the remaining
        engines keep their relative order."""
        found = {}
        ctx = self.context
        try:
            from . import gemini_translate
            if gemini_translate.key_configured() \
                    and not _probe_blocked("gemini"):
                found["gemini"] = lambda seg: \
                    (_engine_translate(
                        gemini_translate, [seg], self.style,
                        ctx)[0],
                     "gemini")
        except Exception:
            pass
        try:
            from . import openrouter_translate
            if openrouter_translate.key_configured():
                def _or(seg, _m=openrouter_translate):
                    by = _engine_translate(
                        _m, [seg], self.style, ctx)
                    return by[0], _m.last_label()
                found["openrouter"] = _or
        except Exception:
            pass
        try:
            from . import xkiro_translate
            if xkiro_translate.key_configured():
                def _xk(seg, _m=xkiro_translate):
                    by = _engine_translate(
                        _m, [seg], self.style, ctx)
                    return by[0], _m.last_label()
                found["xkiro"] = _xk
        except Exception:
            pass
        try:
            from . import openai_translate
            if openai_translate.key_configured() \
                    and not _probe_blocked("openai"):
                found["openai"] = lambda seg: \
                    (_engine_translate(
                        openai_translate, [seg], self.style,
                        ctx)[0],
                     "openai")
        except Exception:
            pass
        order = ("gemini", "openai", "openrouter", "xkiro") \
            if self.admin else \
            ("gemini", "openrouter", "xkiro", "openai")
        return [(name, found[name]) for name in order
                if name in found]

    def one(self, seg):
        """(myanmar_text, provider) for ONE segment; on
        total failure the source text under a
        fallback-source label, which translation_stats
        counts as untranslated."""
        text = (seg.get("text") or "").strip()
        last_err = None
        for name, fn in self._attempts():
            if self.fails.get(name, 0) >= 2:
                continue
            try:
                out, prov = fn(seg)
                out = (out or "").strip()
                if out and not _seg_untranslated(
                        {"text": text, "text_my": out,
                         "provider": prov}):
                    self._note(name, True)
                    return out, prov
                if out:
                    # The engine ECHOED the source — that
                    # is not a translation; count the
                    # failure and move on.
                    last_err = RuntimeError(
                        f"{name} echoed source")
                else:
                    last_err = RuntimeError("empty reply")
                self.fails[name] = self.fails.get(name, 0) + 1
                self._note(name, False, last_err)
            except Exception as e:
                last_err = e
                self.fails[name] = self.fails.get(name, 0) + 1
                self._note(name, False, e)
        return text, f"fallback-source ({last_err})"


# How long a translate probe verdict stays trustworthy (seconds) —
# the same 6 h the app itself uses before it re-checks
# (app.py _translate_probe_maybe).
_PROBE_FRESH_S = 21600


def _probe_blocked(engine):
    """Build 186 (audit fix #4): True when the app's most recent
    translate probe (.translate_probe.json, refreshed by app.py
    at most every 6 h) found this engine REGION-BLOCKED from this
    server and the verdict is still fresh.

    Direct Gemini is location-blocked and direct OpenAI is
    territory-blocked from the VPS (builds 164/168). Their
    failures are fast today, but on a hanging day the Gemini walk
    alone could stack 6 model/version shapes x 120 s ahead of
    the working gateways. While the probe's region-block verdict
    is fresh, jobs skip the attempt entirely; the probe itself
    calls the engines directly (not through here), so the day
    the region changes the probe notices and the skip lifts on
    its own. Only a region-block SIGNATURE skips — a plain
    failure (quota, catalog, network) never does, and a stale or
    missing probe never does."""
    try:
        data = json.loads((PROJECT_ROOT / ".translate_probe.json")
                          .read_text(encoding="utf-8"))
    except Exception:
        return False
    ts = float(data.get("ts") or 0)
    if not ts or time.time() - ts > _PROBE_FRESH_S:
        return False
    if engine == "gemini":
        if data.get("gemini_ok"):
            return False
        blob = ((data.get("gemini_err") or "") + " "
                + (data.get("gemini_models_err") or ""))
        return "not supported" in blob or "FAILED_PRECONDITION" in blob
    if engine == "openai":
        if data.get("openai_ok"):
            return False
        blob = data.get("openai_err") or ""
        return ("unsupported_country_region_territory" in blob
                or "territory not supported" in blob)
    return False


def _probe_engine_ok(engine):
    """Build 189: True when the app's most recent translate probe
    found this engine WORKING and the verdict is still fresh
    (same 6 h freshness as _probe_blocked above).

    The probe (app.py _run_translate_probe) calls the engines
    through the exact client code jobs use — including the VPN
    tunnel (build 189) whenever it is verified up — so a green
    verdict here means the engine can carry a real job right
    now. It is the gate for the chain's one dynamic move: while
    Gemini's verdict is green, Gemini LEADS the chain (it is
    the owner's quality target and costs nothing on his key);
    the moment the verdict is red or stale, the order is the
    build-168 free-first order again, byte for byte."""
    try:
        data = json.loads((PROJECT_ROOT / ".translate_probe.json")
                          .read_text(encoding="utf-8"))
    except Exception:
        return False
    ts = float(data.get("ts") or 0)
    if not ts or time.time() - ts > _PROBE_FRESH_S:
        return False
    return data.get(f"{engine}_ok") is True


def _gemini_attempt(segs, style, report, admin=False,
                    context=None):
    """ONE Gemini attempt with the chain's usual report
    semantics; the translated segment list, or None when the
    engine is unconfigured / probe-skipped / failed (the error
    lands in report["gemini_err"]). Called from the build-189
    lead slot and from Gemini's historical fallback slot — the
    caller guarantees it runs at most once per chunk. Build
    232: `admin` only steers the fill rescue's walk order.
    Build 248: `context` is the chunk carry-over, threaded
    into the attempt and its fill rescue."""
    try:
        from . import gemini_translate
        if gemini_translate.key_configured() \
                and not _probe_blocked("gemini"):
            by_idx = _engine_translate(gemini_translate, segs,
                                       style, context)
            out = [{**seg, "text_my": by_idx[i], "provider": "gemini"}
                   for i, seg in enumerate(segs)]
            return _fill_missing(out, report, style=style,
                                 admin=admin, context=context)
    except Exception as e:
        report["gemini_err"] = str(e)[:120]
    return None


def _openai_attempt(segs, style, report, admin=False,
                    context=None):
    """ONE OpenAI attempt; same contract as _gemini_attempt."""
    try:
        from . import openai_translate
        if openai_translate.key_configured() \
                and not _probe_blocked("openai"):
            by_idx = _engine_translate(openai_translate, segs,
                                       style, context)
            out = [{**seg, "text_my": by_idx[i], "provider": "openai"}
                   for i, seg in enumerate(segs)]
            return _fill_missing(out, report, style=style,
                                 admin=admin, context=context)
    except Exception as e:
        report["openai_err"] = str(e)[:120]
    return None


def _fill_missing(out, report=None, style="subs", admin=False,
                  context=None):
    """No silent gaps (owner complaint 2026-11-09: subtitle lines
    missing from the finished video). Batch endpoints — Gemini
    included — sometimes return an EMPTY string for a segment, and
    write_srt/TTS then skip it, leaving a hole. Retry each empty
    segment once per-segment; the rescue's own last resort is
    the source text, so a line is never dropped silently.

    D1 (2026-11-10): ECHOED segments (batch returned the source text
    unchanged) are rescued the same way — a minority of echoes used
    to ship silently as foreign-language subtitles because only the
    empty case was retried.

    D2 (2026-11-10): fallback-source segments (the main chain gave
    up on them) are rescued too — earlier they were skipped here,
    which made every fill pass blind to exactly the segments a
    throttled job produces most. A fresh rescue has fresh fail
    counters, and by fill time (let alone the second wind's) the
    engine may well have recovered."""
    rescue = None
    for seg in out:
        if not (seg.get("text") or "").strip():
            continue
        empty = not (seg.get("text_my") or "").strip()
        echoed = not empty and _seg_untranslated(seg) \
            and not (seg.get("provider") or "").startswith("fallback-source")
        failed = (seg.get("provider") or "").startswith("fallback-source")
        if empty or echoed or failed:
            if rescue is None:
                rescue = _Rescue(style, admin=admin,
                                 context=context)
            new_my, new_prov = rescue.one(seg)
            # Keep whichever result is actually translated.
            if not _seg_untranslated({**seg, "text_my": new_my,
                                       "provider": new_prov}):
                seg["text_my"], seg["provider"] = new_my, new_prov
            elif empty:
                seg["text_my"], seg["provider"] = new_my, new_prov
    if report is not None and rescue is not None:
        report["fill"] = rescue.report
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


def _write_report(workdir, report):
    try:
        import json
        (workdir / "translate_report.json").write_text(
            json.dumps(report, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _walk_batch(segs, style, admin=False, context=None):
    """The pre-248 walk, unchanged, over ONE batch of segments
    (build 248: one chunk — for a single-chunk job this is the
    whole job, called exactly as before). Returns (out,
    report): out always has exactly len(segs) entries in input
    order — every success path returns its batch result, and
    the final fallback is the per-segment rescue, whose last
    resort is the source text under a fallback-source label.
    report is this batch's own walk report (the caller files
    it under the chunk's entry in translate_report.json).
    `context` is the chunk carry-over (None for chunk 1 and
    for single-chunk jobs), threaded into every engine call,
    fill and rescue below."""
    report = {"rescue": {}, "fill": {}}
    # 0-lead. Gemini FIRST while its probe verdict is green
    # (build 189): direct Gemini is location-blocked from this
    # VPS, but through the owner's VPN tunnel (sing-box on loopback,
    # used ONLY by the Gemini/OpenAI clients) it answers — and
    # the app probe, which exercises the same tunneled client,
    # proves it before any job trusts it. Green probe -> Gemini
    # leads: it is the owner's quality target and free on his
    # key. Red/stale probe -> this block simply does not run
    # and the build-168 free-first order below is untouched.
    # The attempt is remembered so Gemini's historical slot
    # further down never retries it within the same batch.
    gemini_tried = False
    openai_tried = False
    if segs and _probe_engine_ok("gemini"):
        gemini_tried = True
        out = _gemini_attempt(segs, style, report, admin=admin,
                              context=context)
        if out is not None:
            return out, report
    # Build 232: ADMIN jobs lead with Gemini, then OpenAI —
    # ahead of the gateways below. Probe rules still bind: a
    # fresh region-block verdict skips Gemini here exactly
    # like its historical slot does, and an unconfigured
    # engine is simply absent (the attempt returns None and
    # the walk moves on). Both attempts are remembered, so
    # the historical slots below never retry them within the
    # batch; whatever they cannot carry falls through to the
    # existing gateway order untouched.
    if admin and segs:
        if not gemini_tried:
            if _probe_blocked("gemini"):
                report["gemini_skipped"] = \
                    "region-blocked (fresh probe)"
            gemini_tried = True
            out = _gemini_attempt(segs, style, report, admin=True,
                                  context=context)
            if out is not None:
                return out, report
        if _probe_blocked("openai"):
            report["openai_skipped"] = \
                "region-blocked (fresh probe)"
        openai_tried = True
        out = _openai_attempt(segs, style, report, admin=True,
                              context=context)
        if out is not None:
            return out, report
    # 0. OpenRouter batch — the FIRST gateway (build 147; build
    # 168 free-first reorder, owner order 2026-11-13): the owner
    # deferred the paid credit purchase, so the best FREE models
    # lead every walk and paid models sit at the back; the
    # region-blocked direct engines (Gemini/OpenAI) moved behind
    # the working gateways so they stop failing first on every
    # job. OpenRouter is a US aggregator (not region-blocked from
    # the VPS); its walk is ERNIE free -> Gemma 4 free pair ->
    # DeepSeek paid last; story-style prompts apply. Any failure
    # falls through to xKiro.
    if segs:
        try:
            from . import openrouter_translate
            if openrouter_translate.key_configured():
                by_idx = _engine_translate(
                    openrouter_translate, segs, style, context)
                label = openrouter_translate.last_label()
                out = [{**seg, "text_my": by_idx[i],
                        "provider": label}
                       for i, seg in enumerate(segs)]
                out = _fill_missing(out, report, style=style,
                                    admin=admin, context=context)
                return out, report
        except Exception as e:
            report["openrouter_err"] = str(e)[:120]
    # 0b. xKiro batch — second gateway (build 155, owner chose
    # it 2026-11-11): a young OpenAI-compatible gateway whose
    # free tier (Aya / Command A+ / Mistral — leading its walk
    # since the build-168 free-first reorder) plus $5
    # Telegram-verification credit reach DeepSeek V4 Flash
    # cheaper than OpenRouter, DeepSeek pair last in the walk.
    # Any failure falls through to Gemini.
    if segs:
        try:
            from . import xkiro_translate
            if xkiro_translate.key_configured():
                by_idx = _engine_translate(
                    xkiro_translate, segs, style, context)
                label = xkiro_translate.last_label()
                out = [{**seg, "text_my": by_idx[i],
                        "provider": label}
                       for i, seg in enumerate(segs)]
                out = _fill_missing(out, report, style=style,
                                    admin=admin, context=context)
                return out, report
        except Exception as e:
            report["xkiro_err"] = str(e)[:120]
    # 0c. Gemini batch — third (build 136 engine). Direct Gemini
    # is LOCATION-blocked from this VPS (proven build 164), so
    # since build 168 it sits behind the working gateways instead
    # of failing first on every job; the block stays for the day
    # the server region changes. Any failure falls through to
    # OpenAI, with the error kept in the report for diagnosis.
    # Build 186: while the app probe's region-block verdict is
    # fresh, the attempt is skipped outright (see _probe_blocked).
    # Build 189: when the lead slot above already ran the
    # attempt (green probe), this slot is skipped entirely —
    # one attempt per batch, whichever slot it came from.
    if segs and _probe_blocked("gemini") and not gemini_tried:
        report["gemini_skipped"] = "region-blocked (fresh probe)"
    if segs and not gemini_tried:
        out = _gemini_attempt(segs, style, report, admin=admin,
                              context=context)
        if out is not None:
            return out, report
    # 0d. OpenAI batch — fourth engine, when a key is configured.
    # Direct OpenAI is region-blocked from the VPS (403
    # unsupported territory), so since build 168 it sits behind
    # the working gateways like Gemini. Any failure falls
    # through to the per-segment rescue below. Build 186:
    # skipped while the probe's region-block verdict is
    # fresh, like Gemini above.
    # Build 189: OpenAI keeps exactly this slot — after Gemini,
    # never promoted above it; through the tunnel it answers
    # here on the days Gemini itself is down.
    # Build 232: for ADMIN jobs the pre-walk above already ran
    # this attempt in second place; openai_tried keeps it to
    # one attempt per batch, whichever slot it came from.
    if segs and _probe_blocked("openai") and not openai_tried:
        report["openai_skipped"] = "region-blocked (fresh probe)"
    if segs and not openai_tried:
        out = _openai_attempt(segs, style, report, admin=admin,
                              context=context)
        if out is not None:
            return out, report
    # Build 231: the Apps Script slot that used to sit
    # here is removed with the rest of the literal routes —
    # it was literal machine translation through the
    # owner's Google account, and he declined that setup
    # back in 2026-11. The module and its settings
    # endpoints stay on disk, inert.
    # Build 231: the batch slots above are the whole
    # story now — the literal chain (batched web requests
    # + the per-segment web chain) is removed, so whatever
    # reaches here is retried per-segment through the LLM
    # rescue itself, and whatever still fails keeps its
    # source text under a fallback-source label for
    # run_job's guard to reject loudly.
    rescue = _Rescue(style, admin=admin, context=context)
    out = []
    for seg in segs:
        if not (seg.get("text") or "").strip():
            out.append({**seg, "text_my": "",
                        "provider": "empty"})
        else:
            _my, _prov = rescue.one(seg)
            out.append({**seg, "text_my": _my,
                        "provider": _prov})
    report["rescue"] = rescue.report
    out = _fill_missing(out, report, style=style, admin=admin,
                        context=context)
    # D2 "second wind": the 21:19 job died with 24 of ~50
    # segments translated — the working route was throttled
    # mid-job and the first fill pass ran while it was still
    # cooling down. When real progress was made but a
    # remainder is left, wait out the throttle window ONCE
    # and run one more fill pass with a fresh rescue before
    # giving up. Costs under a minute; rescues exactly the
    # near-miss jobs that used to fail whole.
    stats = translation_stats(out)
    if stats["untranslated"] > 0 \
            and stats["total"] - stats["untranslated"] > 0:
        pause = 40.0 if stats["untranslated"] >= 5 else 6.0
        report["second_wind"] = {
            "remaining_before": stats["untranslated"],
            "pause_s": pause}
        time.sleep(pause)
        tmp = {}
        out = _fill_missing(out, tmp, style=style, admin=admin,
                            context=context)
        report["fill2"] = tmp.get("fill", {})
        report["second_wind"]["remaining_after"] = \
            translation_stats(out)["untranslated"]
    return out, report


# --- Build 248: chunked translation ----------------------------
# Primary split rule: a chunk spans at most _CHUNK_SPAN_S of
# audio, measured from the first segment's start to the last
# segment's end. Secondary safety: _CHUNK_MAX_SEGS bounds the
# request when captions are dense (and is the only rule when
# segments carry no timings). A single segment longer than
# the span still forms its own chunk — a chunk is never
# split mid-segment, and chunks are consecutive and complete:
# concatenating them reproduces the input exactly.
_CHUNK_SPAN_S = 600.0
_CHUNK_MAX_SEGS = 120


def _split_chunks(segs):
    """Split `segs` into consecutive chunks per the rules
    above. Returns a list of non-empty segment lists whose
    concatenation is `segs` itself (same dicts, same order)."""
    chunks = []
    cur = []
    t0 = None
    for seg in segs:
        end = seg.get("end")
        if cur and t0 is not None and end is not None \
                and end - t0 > _CHUNK_SPAN_S:
            chunks.append(cur)
            cur = []
            t0 = None
        if cur and len(cur) >= _CHUNK_MAX_SEGS:
            chunks.append(cur)
            cur = []
            t0 = None
        if not cur:
            start = seg.get("start")
            t0 = start if start is not None else end
        cur.append(seg)
    if cur:
        chunks.append(cur)
    return chunks


# Glossary extraction (build 248): the standing prompt rules
# keep every personal/place/brand name in Latin letters, so
# the names established so far are exactly the capitalised
# Latin runs in the source text and in the Burmese output.
# Single capitalised words are filtered against common
# sentence-start words; multi-word capitalised sequences and
# ALLCAPS tokens are almost always names or brands.
_NAME_MULTI = re.compile(r"[A-Z][A-Za-z'’-]*(?:\s+[A-Z][A-Za-z'’-]*)+")
_NAME_SINGLE = re.compile(r"[A-Z][a-z]{2,}")
_NAME_CAPS = re.compile(r"\b[A-Z]{2,}\b")
_NAME_STOP = {
    "The", "And", "But", "Then", "Now", "This", "That",
    "There", "Here", "What", "Why", "How", "When", "Where",
    "Who", "Yes", "Well", "Just", "Have", "Has", "Had",
    "Will", "Would", "Can", "Could", "Should", "Must",
    "They", "Their", "Them", "Your", "Yours", "His", "Her",
    "Hers", "Our", "Ours", "She", "Him", "You", "Not", "All",
    "One", "Two", "After", "Before", "When", "While", "If",
    "In", "On", "At", "To", "Of", "For", "With", "As", "By",
    "It", "Its", "He", "We", "Me", "My", "Do", "Did", "Was",
    "Were", "Are", "Been", "Being", "From", "Into", "Over",
    "Under", "About", "Again", "Once", "Very", "Too", "So",
    "Oh", "Ah", "No",
}
_GLOSSARY_MAX = 48
_CONTEXT_TAIL = 6
_CONTEXT_MAX_CHARS = 2400


def _glossary_add(found, text):
    """Collect name candidates from one text into `found`
    (an ordered dict used as an ordered set)."""
    if not text:
        return
    for m in _NAME_MULTI.finditer(text):
        found.setdefault(m.group(0).strip(), True)
    for m in _NAME_CAPS.finditer(text):
        found.setdefault(m.group(0), True)
    for m in _NAME_SINGLE.finditer(text):
        w = m.group(0)
        if w not in _NAME_STOP:
            found.setdefault(w, True)


def _chunk_context(done_out, glossary):
    """The carry-over block for the next chunk (build 248):
    the story so far as the most recent translated lines
    (source -> the Burmese rendering used, so the next chunk
    matches voice and wording), plus the accumulated names
    glossary. Returns None when there is nothing to carry
    (every prior segment empty and no names yet) — the next
    chunk then runs context-free, exactly like chunk 1."""
    tail = [s for s in done_out
            if (s.get("text") or "").strip()][-_CONTEXT_TAIL:]
    if not tail and not glossary:
        return None
    lines = ["CONTEXT — this video's earlier parts "
             "(background ONLY: do not translate this block "
             "and do not include any of it in your output; "
             "the segments to translate follow it):"]
    if tail:
        lines.append("Story so far — the most recent lines, "
                     "source then the Burmese rendering used:")
        for s in tail:
            lines.append("- " + (s.get("text") or "").strip()
                         + " → "
                         + (s.get("text_my") or "").strip())
    if glossary:
        lines.append(
            "Names and terms already established — keep "
            "these EXACT Latin-letter forms wherever the "
            "same names or terms appear again: "
            + ", ".join(glossary))
    return "\n".join(lines)[:_CONTEXT_MAX_CHARS]


def _merge_counts(dst, src):
    """Merge one chunk's rescue/fill report ({engine: {ok,
    fail, last}}) into the job-level aggregate run_job's
    guard reads for its failure detail."""
    for name, r in (src or {}).items():
        d = dst.setdefault(name, {"ok": 0, "fail": 0, "last": ""})
        d["ok"] = d.get("ok", 0) + (r.get("ok") or 0)
        d["fail"] = d.get("fail", 0) + (r.get("fail") or 0)
        if r.get("last"):
            d["last"] = r["last"]


def translate_segments(workdir, style="subs", admin=False):
    ensure_system_ca()
    workdir = Path(workdir)
    segs = load_segments(workdir / "segments_en.json")
    chunks = _split_chunks(segs)
    single = len(chunks) <= 1
    report = {"rescue": {}, "fill": {}}
    chunk_entries = []
    out_all = []
    glossary_found = {}
    offset = 0
    for ci, chunk in enumerate(chunks):
        glossary = list(glossary_found)[:_GLOSSARY_MAX]
        context = None if ci == 0 else _chunk_context(
            out_all, glossary)
        out, crep = _walk_batch(chunk, style, admin=admin,
                                context=context)
        # Segment integrity is absolute (build 248): the
        # chunk's output is validated for exact count and
        # order against its input BEFORE acceptance. The
        # walk's contract already guarantees this; if it is
        # ever broken, rebuild the chunk per-segment through
        # the rescue (whose output is exact by construction)
        # instead of accepting a shifted list.
        if len(out) != len(chunk) or any(
                (o.get("text") or "") != (c.get("text") or "")
                or o.get("start") != c.get("start")
                or o.get("end") != c.get("end")
                for o, c in zip(out, chunk)):
            rescue = _Rescue(style, admin=admin,
                             context=context)
            fixed = []
            for seg in chunk:
                if not (seg.get("text") or "").strip():
                    fixed.append({**seg, "text_my": "",
                                  "provider": "empty"})
                else:
                    _my, _prov = rescue.one(seg)
                    fixed.append({**seg, "text_my": _my,
                                  "provider": _prov})
            crep["chunk_rescue"] = rescue.report
            out = fixed
        providers = {}
        for o in out:
            if (o.get("text") or "").strip():
                p = o.get("provider") or ""
                providers[p] = providers.get(p, 0) + 1
        winner = max(providers, key=providers.get) \
            if providers else ""
        chunk_entries.append({
            "index": ci,
            "seg_from": offset,
            "seg_to": offset + len(chunk),
            "count": len(chunk),
            "start_s": chunk[0].get("start"),
            "end_s": chunk[-1].get("end"),
            "context": bool(context),
            "winner": winner,
            "providers": providers,
            "walk": crep,
        })
        if single:
            # One chunk = the pre-248 job: the top-level
            # report is exactly the walk's report, as before.
            report.update(crep)
        else:
            for sec in ("rescue", "fill", "fill2"):
                if crep.get(sec):
                    _merge_counts(report.setdefault(sec, {}),
                                  crep[sec])
        for c, o in zip(chunk, out):
            _glossary_add(glossary_found, c.get("text"))
            _glossary_add(glossary_found, o.get("text_my"))
        out_all.extend(out)
        offset += len(chunk)
    if not single:
        report["chunked"] = True
    report["chunks"] = chunk_entries
    _write_report(workdir, report)
    save_segments(out_all, workdir / "segments_my.json")
    write_srt(out_all, workdir / "subs_my.srt")
    return out_all


if __name__ == "__main__":
    res = translate_segments(Path(sys.argv[1]))
    provs = {}
    for r in res:
        provs[r["provider"]] = provs.get(r["provider"], 0) + 1
    print(f"{len(res)} segments translated, providers: {provs}")
