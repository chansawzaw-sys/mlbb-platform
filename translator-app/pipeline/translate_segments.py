"""Step 3: translate each segment to Myanmar (Burmese).

Build 264 (owner decision 2026-11-26): translation is
OpenAI-ONLY. The owner's words: OpenAI in front; Gemini no
longer translates — it stays for voice only, left in place
so it can be switched back on conveniently later. Both the
batch/chunk walk and the per-segment rescue now call ONLY
pipeline/openai_translate (its own model walk —
gpt-4o-mini -> gpt-4.1-mini, tunnel-routed — unchanged).
Gemini / OpenRouter / xKiro leave the walk; their modules,
keys, settings cards and the probe machinery stay in the
tree, dormant, and Gemini TTS is untouched (Gemini leaves
TRANSLATION only). The build-232 admin-vs-user order
difference is gone — one walk for everyone. If OpenAI
fails a chunk/segment after its model walk, the segments
stay untranslated and the build-124 guard in run_job fails
the job loudly (Burmese error, zero charge) — no engine is
silently substituted.

RE-ENABLE GATE: the pre-264 walk described below is
preserved verbatim as _walk_batch_legacy plus the legacy
branch of _Rescue._attempts. Setting TRANSLATE_CHAIN=legacy
in the project env (.env or process env — also flippable
from Admin -> the OpenAI card, via
/api/settings/translate-chain) restores it exactly: probe
rules, admin variant and all. The gate is read ONCE at
import into the module constant _CHAIN, so a flip takes
effect on the next app start. Any other value (or unset)
means "openai", the default.

Provider walk (LEGACY gate only) — LLM engines only (build 231 removed the
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
     lines, source -> Burmese) plus a glossary of the names
     established so far, threaded into the engines' shared
     prompt builder so names and story stay consistent
     across chunk boundaries. Segment count
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

  Build 252 (owner decision 2026-11-25, REVERSING the
     builds 153-154 rule): names are no longer kept in
     Latin letters — every personal/place/brand name is
     written in Burmese script, transliterated by SOUND
     (a Chinese name by its pinyin sound, e.g. Lin Feng
     -> လင်ဖုန်း), in ONE fixed form per name, never
     translated by meaning. The shared prompt source
     (pipeline/gemini_translate.py PROMPT_SUBS/PROMPT_DUB,
     imported by the OpenAI/OpenRouter/xKiro builders)
     carries the new rule, as do the Recap/Commentary/
     Story prose prompts. The build-248 glossary follows
     the same flip: it no longer pins Latin forms — it
     pins the CHOSEN BURMESE FORMS, recovered from the
     completed output by elimination (see
     _established_name_entries), rendered as
     source-name -> Burmese-form pairs alongside the
     story-so-far lines, which already show each recent
     name's rendering in place.

  Build 265 (owner complaint 2026-11-27: on a 2 h job the
     translate stage took 51:49 — 12 chunks walked strictly
     one after another at ~4:19 each, now the largest stage
     since Groq fixed transcription in build 263): chunked
     jobs translate their chunks with BOUNDED CONCURRENCY —
     TRANSLATE_CHUNK_PARALLEL chunks in flight (default 3,
     hard cap 4, read once at import like the chain gate;
     1 restores the pre-265 serial walk byte for byte, and
     a single-chunk job is always the old path). Chunk 0
     still runs first and alone (context-free, exactly as
     before) to seed the glossary. Each later chunk, when
     it STARTS, snapshots its completed predecessors (in
     index order) and builds the same carry-over context
     the serial walk would have used over that subset —
     story-so-far pairs plus established-names pins; when
     its immediate predecessor has not finished, the
     predecessor's SOURCE-side tail is appended (labelled
     source-only) so the story thread is never missing.
     The coherence trade-off is honest: a chunk cannot see
     predecessors that are still in flight, so a name first
     rendered by two parallel chunks could diverge before
     either pins it. The invariant — one source name, one
     Burmese form, in EVERY chunk — is therefore enforced
     after the merge by _glossary_consistency_pass: the
     build-252 elimination run job-wide finds every form a
     name is actually rendered in; the earliest chunk's
     form wins and losing forms are replaced whole-token,
     only inside segments whose source contains the name.
     The pass is recorded in translate_report.json
     ("glossary_pass"), as is each chunk's wall time and
     the predecessors its context came from ("ctx_from").
     One chunk's failure never stalls or cancels the
     others: the walk's own rescue/fallback shapes stay
     inside the chunk, an unexpected worker error degrades
     that chunk alone to the rescue rebuild (then to
     fallback-source entries), and run_job's guard judges
     the merged result exactly as before. Per-chunk calls
     stay sequential inside a chunk (batch, then fill, then
     rescue), so in-flight OpenAI calls never exceed the
     chunk bound — no retry storm. The per-segment rescue
     is deliberately NOT batched: on the success path the
     batch call carries the chunk and rescue only fills
     gaps, and the rescue tail is shared verbatim with the
     legacy gate, whose behaviour build 264 froze.

CLI: python -m pipeline.translate_segments <workdir>
Reads segments_en.json, writes segments_my.json + subs_my.srt.
Build 271: the SRT is written through pipeline.cue_timing —
from the word-corrected cues when the job has word
timestamps, from the segments (original timings) when not.
"""
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import cue_timing, quota
from .common import (PROJECT_ROOT, ensure_system_ca, load_segments,
                     save_segments)


# --- Build 264: the translation-chain gate --------------------
# "openai" (default): the walk and the rescue call ONLY
# pipeline/openai_translate. "legacy": the pre-264 walk,
# preserved verbatim below. Read ONCE at import from the
# process env, falling back to the project .env — the same
# two sources, in the same order, as
# openai_translate.get_api_key. A flip therefore takes
# effect on the next app start, never mid-job.

def _read_chain_gate():
    v = (os.environ.get("TRANSLATE_CHAIN") or "").strip().lower()
    if not v:
        try:
            env_file = PROJECT_ROOT / ".env"
            if env_file.exists():
                for line in env_file.read_text(
                        encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("TRANSLATE_CHAIN="):
                        v = line.split("=", 1)[1].strip() \
                            .strip('"').strip("'").lower()
                        break
        except Exception:
            v = ""
    return "legacy" if v == "legacy" else "openai"


_CHAIN = _read_chain_gate()


def chain_mode():
    """The chain this process booted with: "openai" or
    "legacy" (surfaced by /api/settings/translate-chain so
    the Admin card can show what is actually live versus
    what is saved for the next start)."""
    return _CHAIN


# --- Build 265: the chunk-concurrency knob -------------------
# TRANSLATE_CHUNK_PARALLEL — how many chunks of a chunked
# job may be in flight at once. Read ONCE at import from
# the process env, falling back to the project .env — the
# same two sources, in the same order, as the chain gate
# above — so a change takes effect on the next app start,
# never mid-job. Default 3, hard cap 4: enough to collapse
# the serial wall time (the 51:49 of the 2026-11-27 2 h
# job), small enough that OpenAI rate-limit pressure and
# the tunnel stay calm. 1 = the pre-265 serial walk.
# Junk values fall back to the default rather than failing
# a job over a config typo.

def _read_chunk_parallel():
    raw = (os.environ.get("TRANSLATE_CHUNK_PARALLEL")
           or "").strip()
    if not raw:
        try:
            env_file = PROJECT_ROOT / ".env"
            if env_file.exists():
                for line in env_file.read_text(
                        encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith(
                            "TRANSLATE_CHUNK_PARALLEL="):
                        raw = line.split("=", 1)[1].strip() \
                            .strip('"').strip("'")
                        break
        except Exception:
            raw = ""
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = 3
    return max(1, min(4, n))


_CHUNK_PARALLEL = _read_chunk_parallel()


def chunk_parallel():
    """The chunk concurrency this process booted with
    (1..4; diagnostics/proofs, like chain_mode)."""
    return _CHUNK_PARALLEL


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
    story voice. Build 264: under the default gate the
    rescue is OpenAI-only (see _attempts_openai); the
    multi-engine behaviour described above is the legacy
    gate's, preserved verbatim in _attempts_legacy."""

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
        """(engine, fn) pairs in walk order — the gate
        decides which walk (build 264): OpenAI-only by
        default, the pre-264 multi-engine walk under
        TRANSLATE_CHAIN=legacy."""
        if _CHAIN == "legacy":
            return self._attempts_legacy()
        return self._attempts_openai()

    def _attempts_openai(self):
        """Build 264: OpenAI is the ONLY rescue engine. No
        probe skip — the probe machinery serves the legacy
        walk, and skipping the only engine on a stale
        verdict would fail every segment without an attempt.
        Unconfigured -> no attempts -> one() returns the
        source under fallback-source, exactly the loud-
        failure shape the guard expects."""
        found = {}
        ctx = self.context
        try:
            from . import openai_translate
            if openai_translate.key_configured():
                found["openai"] = lambda seg: \
                    (_engine_translate(
                        openai_translate, [seg], self.style,
                        ctx)[0],
                     "openai")
        except Exception:
            pass
        return [(name, found[name]) for name in ("openai",)
                if name in found]

    def _attempts_legacy(self):
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
            except quota.QuotaWallError:
                # Build 275: a hard quota/billing wall is not
                # a per-segment failure to route around — the
                # whole job fails fast with the wall's message.
                raise
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
                    context=None, probe_skip=True):
    """ONE OpenAI attempt; same contract as _gemini_attempt.
    Build 264: `probe_skip` is honoured only by the legacy
    walk — the OpenAI-only walk passes probe_skip=False,
    because skipping the ONLY engine on a probe verdict
    would fail the chunk without an attempt (the probe
    machinery stays in the tree for the legacy gate)."""
    try:
        from . import openai_translate
        if openai_translate.key_configured() \
                and not (probe_skip and _probe_blocked("openai")):
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


def _rescue_tail(segs, style, report, admin=False,
                 context=None):
    """The shared end of every walk (extracted verbatim from
    the pre-264 _walk_batch tail in build 264): whatever the
    batch slots could not carry is retried per-segment
    through the rescue (gate-aware — OpenAI-only by default,
    the legacy engine set under TRANSLATE_CHAIN=legacy), and
    whatever still fails keeps its source text under a
    fallback-source label for run_job's guard to reject
    loudly. Ends with the D2 "second wind": when real
    progress was made but a remainder is left, wait out the
    throttle window ONCE and run one more fill pass with a
    fresh rescue before giving up."""
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


def _walk_batch_openai(segs, style, admin=False, context=None):
    """Build 264: the OpenAI-only walk over ONE batch of
    segments (build 248: one chunk). Same (out, report)
    contract as the legacy walk: out always has exactly
    len(segs) entries in input order, and the final fallback
    is the per-segment rescue (OpenAI-only under this gate).
    `admin` is accepted for signature parity and changes
    nothing — one walk for everyone. OpenAI's own model walk
    (gpt-4o-mini -> gpt-4.1-mini, tunnel-routed) runs inside
    openai_translate; if it fails the batch, the error lands
    in report["openai_err"] and the rescue tail takes over —
    no other engine is substituted, ever."""
    report = {"rescue": {}, "fill": {}}
    if segs:
        out = _openai_attempt(segs, style, report, admin=admin,
                              context=context, probe_skip=False)
        if out is not None:
            return out, report
    return _rescue_tail(segs, style, report, admin=admin,
                        context=context)


def _walk_batch(segs, style, admin=False, context=None):
    """Walk selection (build 264): the gate read at import
    decides — OpenAI-only by default, the pre-264 walk under
    TRANSLATE_CHAIN=legacy."""
    if _CHAIN == "legacy":
        return _walk_batch_legacy(segs, style, admin=admin,
                                  context=context)
    return _walk_batch_openai(segs, style, admin=admin,
                              context=context)


def _walk_batch_legacy(segs, style, admin=False, context=None):
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
    fill and rescue below. Build 264: reachable only under
    TRANSLATE_CHAIN=legacy — preserved verbatim as the
    re-enable path."""
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
    # run_job's guard to reject loudly. Build 264: that
    # tail now lives in _rescue_tail, shared verbatim with
    # the OpenAI-only walk.
    return _rescue_tail(segs, style, report, admin=admin,
                        context=context)


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


# Glossary extraction (build 248; flipped by build 252):
# the names established so far are identified by the
# capitalised Latin runs in the SOURCE text — since build
# 252 the output writes every name in Burmese script, so
# the source side is where a name's identity is visible.
# Single capitalised words are filtered against common
# sentence-start words; multi-word capitalised sequences
# and ALLCAPS tokens are almost always names or brands.
# The CHOSEN BURMESE FORM for each name is recovered from
# the completed output by elimination (see
# _established_name_entries below), never from the source.
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


def _name_candidates(text):
    """The set of name candidates in ONE source text (the
    same extraction _glossary_add accumulates job-wide)."""
    found = {}
    _glossary_add(found, text)
    return set(found)


# Build 252: recovering the chosen Burmese form of a name
# from completed output. Burmese tokens are the maximal
# Myanmar-script runs of a rendering; grammatical tokens
# that can never be a transliterated name are stoplisted,
# and the pairing itself is by elimination (see
# _established_name_entries).
_MY_TOKEN = re.compile(r"[\u1000-\u109f]+")
_NAME_FORM_STOP = {
    "သည်", "တယ်", "ပါ", "ကို", "မှာ", "နှင့်", "တဲ့",
    "ရဲ့", "တွေ", "များ", "ဖြစ်", "ရှိ", "သူ", "သူ့",
    "ဒီ", "အဲ", "ဟို", "လို့", "ပြီး", "နေ", "ခဲ့",
    "မယ်", "သော", "တို့", "လည်း", "ပဲ", "မှ",
    "ရင်", "တော့", "ဘူး", "မ", "ခြင်း", "စွာ",
}


def _burmese_tokens(text):
    """The Myanmar-script tokens of one translated line,
    in order (runs of at least two codepoints — a lone
    consonant is a fragment, never a name form)."""
    return [t for t in _MY_TOKEN.findall(text or "")
            if len(t) >= 2]


def _established_name_entries(history, src_names):
    """The names list for the next chunk's context (build
    252): one entry per established source name, in the
    order the names first appeared — "Source Name ->
    Burmese form" when the form can be determined, the
    bare source name when it cannot (the story-so-far
    lines and the context instruction then carry it: keep
    the Burmese form already used for it).

    `history` is one (names-in-segment, Burmese tokens of
    its rendering) record per completed segment. A name's
    form is determined by elimination: the tokens present
    in EVERY rendering of a segment containing the name,
    minus every token that appears in a rendering whose
    source does NOT contain the name (ordinary vocabulary
    and other names' forms live there), minus the
    grammatical stoplist, minus forms already claimed by
    an earlier name. Exactly one survivor is a confident
    pair; zero or several survivors stay unpaired rather
    than guess."""
    per_seg = [(names, set(toks)) for names, toks in history]
    entries = []
    claimed = set()
    for name in src_names:
        occ = [toks for names, toks in per_seg if name in names]
        if not occ:
            continue
        cand = set.intersection(*occ) if len(occ) > 1 \
            else set(occ[0])
        for names, toks in per_seg:
            if name not in names:
                cand -= toks
        cand -= _NAME_FORM_STOP
        cand -= claimed
        if len(cand) == 1:
            form = next(iter(cand))
            claimed.add(form)
            entries.append(f"{name} → {form}")
        else:
            entries.append(name)
    return entries


def _chunk_context(done_out, name_entries):
    """The carry-over block for the next chunk (build 248):
    the story so far as the most recent translated lines
    (source -> the Burmese rendering used, so the next chunk
    matches voice and wording), plus the established names
    list (build 252: source name -> the chosen Burmese
    form, from _established_name_entries). Returns None
    when there is nothing to carry (every prior segment
    empty and no names yet) — the next chunk then runs
    context-free, exactly like chunk 1."""
    tail = [s for s in done_out
            if (s.get("text") or "").strip()][-_CONTEXT_TAIL:]
    if not tail and not name_entries:
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
    if name_entries:
        lines.append(
            "Names already established — names are written "
            "in Burmese transliteration: where a pair is "
            "shown (source name → Burmese form), that is "
            "the EXACT Burmese form already chosen — use "
            "that same Burmese form wherever the name "
            "appears again; for a name shown without a "
            "pair, keep the Burmese form already used for "
            "it in the lines above: "
            + ", ".join(name_entries))
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


def _run_one_chunk(chunk, style, admin=False, context=None):
    """ONE chunk's walk + integrity rebuild (build 248,
    extracted verbatim in build 265 so the serial loop and
    the parallel workers share it): returns (out, crep)
    with exactly len(chunk) entries in input order —
    segment integrity is absolute, so if the walk's output
    ever breaks exact count/order against its input, the
    chunk is rebuilt per-segment through the rescue (exact
    by construction) instead of accepting a shifted list,
    and the rebuild is recorded as crep["chunk_rescue"]."""
    out, crep = _walk_batch(chunk, style, admin=admin,
                            context=context)
    if len(out) != len(chunk) or any(
            (o.get("text") or "") != (c.get("text") or "")
            or o.get("start") != c.get("start")
            or o.get("end") != c.get("end")
            for o, c in zip(out, chunk)):
        rescue = _Rescue(style, admin=admin, context=context)
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
    return out, crep


def _chunk_entry(ci, offset, chunk, out, crep, context,
                 extra=None):
    """One chunk's translate_report.json entry (build 248
    shape; build 265: `extra` carries the parallel path's
    additive fields — the serial path passes none, so its
    entries keep the old shape exactly)."""
    providers = {}
    for o in out:
        if (o.get("text") or "").strip():
            p = o.get("provider") or ""
            providers[p] = providers.get(p, 0) + 1
    winner = max(providers, key=providers.get) \
        if providers else ""
    entry = {
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
    }
    if extra:
        entry.update(extra)
    return entry


# --- Build 265: the parallel chunk machinery -----------------

def _parallel_context(ci, chunks, preds, results):
    """The carry-over context for a parallel chunk: the
    serial walk's context (_chunk_context: story-so-far
    pairs + established-names pins, from
    _established_name_entries) computed over the chunk's
    COMPLETED predecessors only — `preds` is their index
    list in order, `results` maps index -> (out, crep,
    meta) for them; stored outputs are never mutated
    before the merge, so this reads them lock-free. When
    the immediate predecessor (ci - 1) is NOT among them,
    its SOURCE-side tail is appended as a clearly labelled
    source-only block, so the story thread is present even
    while that chunk is still in flight. Returns None when
    there is nothing to carry at all."""
    done_out = []
    glossary_found = {}
    history = []
    for i in preds:
        cout = results[i][0]
        for c, o in zip(chunks[i], cout):
            _glossary_add(glossary_found, c.get("text"))
            history.append((_name_candidates(c.get("text")),
                            _burmese_tokens(o.get("text_my"))))
        done_out.extend(cout)
    name_entries = _established_name_entries(
        history, list(glossary_found))[:_GLOSSARY_MAX]
    parts = []
    base = _chunk_context(done_out, name_entries)
    if base:
        parts.append(base)
    if ci - 1 not in preds:
        tail = [s for s in chunks[ci - 1]
                if (s.get("text") or "").strip()][-_CONTEXT_TAIL:]
        if tail:
            lines = ["Previous lines in the original — the "
                     "part immediately before yours, not yet "
                     "translated when this request was made "
                     "(background ONLY for story flow: do "
                     "not translate this block and do not "
                     "include any of it in your output):"]
            for s in tail:
                lines.append("- " + (s.get("text") or "").strip())
            parts.append("\n".join(lines))
    if not parts:
        return None
    return "\n".join(parts)[:_CONTEXT_MAX_CHARS]


def _glossary_consistency_pass(pairs):
    """Post-merge glossary consistency (build 265) — the
    parallel scheme's guarantee that ONE source name ends
    up in ONE Burmese form in EVERY chunk. `pairs` is the
    full (source_seg, out_seg) list in segment order; the
    out_segs are mutated in place where a form is replaced.

    For each established name (first-appearance order,
    capped at _GLOSSARY_MAX like the context glossary), the
    candidate forms come from the build-252 elimination
    run job-wide: Burmese tokens that appear in a rendering
    of a segment containing the name and in NO rendering of
    a segment without it, minus the grammatical stoplist
    and minus forms already claimed as an earlier name's
    winner (the build-252 claimed-forms rule). Zero or
    one candidate = nothing to reconcile (already
    consistent, or the form cannot be isolated — left
    alone rather than guessed at). A name appearing in
    EVERY segment is skipped outright: with no contrast
    set the elimination cannot isolate its form at all.
    Two or more = parallel chunks rendered the name
    differently before either could pin it: the form used
    by the EARLIEST segment in chunk order wins (the same
    first-wins rule the serial glossary applies), and
    every losing candidate token is replaced by the
    winner — whole-token only, and only inside segments
    whose source contains the name.
    Returns (pins, replaced) for the report."""
    glossary_found = {}
    for c, _o in pairs:
        _glossary_add(glossary_found, c.get("text"))
    names = list(glossary_found)[:_GLOSSARY_MAX]
    src_names = [_name_candidates(c.get("text"))
                 for c, _o in pairs]
    pins = {}
    replaced = {}
    claimed = set()
    for name in names:
        with_idx = [i for i, ns in enumerate(src_names)
                    if name in ns]
        if not with_idx or len(with_idx) == len(pairs):
            continue
        a = set()
        for i in with_idx:
            a.update(_burmese_tokens(pairs[i][1].get("text_my")))
        b = set()
        for i, ns in enumerate(src_names):
            if name not in ns:
                b.update(_burmese_tokens(pairs[i][1].get("text_my")))
        cand = a - b - _NAME_FORM_STOP - claimed
        if len(cand) < 2:
            continue
        winner = None
        for i in with_idx:
            for t in _burmese_tokens(pairs[i][1].get("text_my")):
                if t in cand:
                    winner = t
                    break
            if winner:
                break
        if winner is None:
            continue
        pins[name] = winner
        claimed.add(winner)
        for loser in sorted(cand - {winner}):
            n = 0
            for i in with_idx:
                o = pairs[i][1]
                if loser in _burmese_tokens(o.get("text_my")):
                    o["text_my"] = _MY_TOKEN.sub(
                        lambda m: winner
                        if m.group(0) == loser else m.group(0),
                        o.get("text_my") or "")
                    n += 1
            if n:
                replaced[loser] = replaced.get(loser, 0) + n
    return pins, replaced


def _run_chunks_parallel(chunks, style, admin=False):
    """Translate every chunk of a multi-chunk job with
    _CHUNK_PARALLEL chunks in flight (build 265). Returns
    (results, pass_info): results maps chunk index ->
    (out, crep, meta) with meta {"secs", "ctx_from",
    "context"}; pass_info is the consistency pass's
    {"pins", "replaced"} record. Chunk 0 runs first, alone
    and context-free — the serial seed. A worker NEVER
    raises: the walk's own failure shapes stay inside its
    chunk, and an unexpected error degrades that chunk
    alone to the rescue rebuild, then to fallback-source
    entries — the merged list is always complete and in
    order, and run_job's guard judges it as before."""
    lock = threading.Lock()
    done = set()
    results = {}

    def run_chunk(ci, context):
        t0 = time.time()
        try:
            out, crep = _run_one_chunk(
                chunks[ci], style, admin=admin, context=context)
        except quota.QuotaWallError:
            # Build 275: the never-raise contract below exists
            # so one chunk's ordinary failure cannot sink the
            # others — a hard quota wall is job-fatal by
            # definition, so it breaks the contract on purpose
            # and fails the whole job fast with its message.
            raise
        except Exception as e:
            crep = {"parallel_error": str(e)[:120]}
            try:
                rescue = _Rescue(style, admin=admin,
                                 context=context)
                out = []
                for seg in chunks[ci]:
                    if not (seg.get("text") or "").strip():
                        out.append({**seg, "text_my": "",
                                    "provider": "empty"})
                    else:
                        _my, _prov = rescue.one(seg)
                        out.append({**seg, "text_my": _my,
                                    "provider": _prov})
                crep["chunk_rescue"] = rescue.report
            except quota.QuotaWallError:
                raise
            except Exception as e2:
                crep["parallel_error2"] = str(e2)[:120]
                out = []
                for seg in chunks[ci]:
                    if (seg.get("text") or "").strip():
                        out.append({
                            **seg, "text_my": seg.get("text") or "",
                            "provider": "fallback-source "
                                        "(parallel worker failed)"})
                    else:
                        out.append({**seg, "text_my": "",
                                    "provider": "empty"})
        return out, crep, round(time.time() - t0, 3)

    out0, crep0, secs0 = run_chunk(0, None)
    results[0] = (out0, crep0, {"secs": secs0, "ctx_from": [],
                                "context": False})
    done.add(0)

    def worker(ci):
        with lock:
            preds = sorted(i for i in done if i < ci)
            snap = {i: results[i] for i in preds}
        try:
            context = _parallel_context(ci, chunks, preds, snap)
        except Exception:
            context = None
        out, crep, secs = run_chunk(ci, context)
        with lock:
            results[ci] = (out, crep, {"secs": secs,
                                       "ctx_from": preds,
                                       "context": bool(context)})
            done.add(ci)

    if len(chunks) > 1:
        with ThreadPoolExecutor(
                max_workers=_CHUNK_PARALLEL) as ex:
            list(ex.map(worker, range(1, len(chunks))))
    pairs = []
    for ci, chunk in enumerate(chunks):
        pairs.extend(zip(chunk, results[ci][0]))
    pins, replaced = _glossary_consistency_pass(pairs)
    return results, {"pins": pins, "replaced": replaced}


def translate_segments(workdir, style="subs", admin=False):
    ensure_system_ca()
    workdir = Path(workdir)
    segs = load_segments(workdir / "segments_en.json")
    chunks = _split_chunks(segs)
    single = len(chunks) <= 1
    report = {"rescue": {}, "fill": {}}
    chunk_entries = []
    out_all = []
    offset = 0
    if not single and _CHUNK_PARALLEL > 1:
        # Build 265: bounded-concurrency chunk translation.
        # Assembly is in chunk order and the report merging
        # matches the serial multi-chunk branch exactly; the
        # parallel path only adds its additive fields.
        results, pass_info = _run_chunks_parallel(
            chunks, style, admin=admin)
        report["parallel"] = {"workers": _CHUNK_PARALLEL,
                              "chunks": len(chunks)}
        report["glossary_pass"] = pass_info
        for ci, chunk in enumerate(chunks):
            out, crep, meta = results[ci]
            chunk_entries.append(_chunk_entry(
                ci, offset, chunk, out, crep, meta["context"],
                extra={"secs": meta["secs"], "parallel": True,
                       "ctx_from": meta["ctx_from"]}))
            for sec in ("rescue", "fill", "fill2"):
                if crep.get(sec):
                    _merge_counts(report.setdefault(sec, {}),
                                  crep[sec])
            out_all.extend(out)
            offset += len(chunk)
        report["chunked"] = True
        report["chunks"] = chunk_entries
        _write_report(workdir, report)
        save_segments(out_all, workdir / "segments_my.json")
        cue_timing.write_subs_srt(workdir, out_all,
                                  workdir / "subs_my.srt")
        return out_all
    glossary_found = {}
    history = []
    for ci, chunk in enumerate(chunks):
        name_entries = _established_name_entries(
            history, list(glossary_found))[:_GLOSSARY_MAX]
        context = None if ci == 0 else _chunk_context(
            out_all, name_entries)
        out, crep = _run_one_chunk(chunk, style, admin=admin,
                                   context=context)
        chunk_entries.append(_chunk_entry(
            ci, offset, chunk, out, crep, context))
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
            history.append((_name_candidates(c.get("text")),
                            _burmese_tokens(o.get("text_my"))))
        out_all.extend(out)
        offset += len(chunk)
    if not single:
        report["chunked"] = True
    report["chunks"] = chunk_entries
    _write_report(workdir, report)
    save_segments(out_all, workdir / "segments_my.json")
    cue_timing.write_subs_srt(workdir, out_all,
                              workdir / "subs_my.srt")
    return out_all


if __name__ == "__main__":
    res = translate_segments(Path(sys.argv[1]))
    provs = {}
    for r in res:
        provs[r["provider"]] = provs.get(r["provider"], 0) + 1
    print(f"{len(res)} segments translated, providers: {provs}")
