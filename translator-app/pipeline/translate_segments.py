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

CLI: python -m pipeline.translate_segments <workdir>
Reads segments_en.json, writes segments_my.json + subs_my.srt (original timings).
"""
import json
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


class _Rescue:
    """Per-segment rescue through the LLM engines, with the
    old chain's memory discipline: an engine that fails
    twice is skipped for the rest of the pass, and every
    call's outcome is recorded in self.report for the job's
    translate_report.json. Gemini's lead rules are
    preserved (configured + the probe's fresh region-block
    skip); OpenAI keeps the same skip."""

    def __init__(self, style="subs"):
        self.style = style
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
        probe-skipped engines are simply absent."""
        out = []
        try:
            from . import gemini_translate
            if gemini_translate.key_configured() \
                    and not _probe_blocked("gemini"):
                out.append(("gemini", lambda seg:
                            (gemini_translate.translate_all(
                                [seg], style=self.style)[0],
                             "gemini")))
        except Exception:
            pass
        try:
            from . import openrouter_translate
            if openrouter_translate.key_configured():
                def _or(seg, _m=openrouter_translate):
                    by = _m.translate_all(
                        [seg], style=self.style)
                    return by[0], _m.last_label()
                out.append(("openrouter", _or))
        except Exception:
            pass
        try:
            from . import xkiro_translate
            if xkiro_translate.key_configured():
                def _xk(seg, _m=xkiro_translate):
                    by = _m.translate_all(
                        [seg], style=self.style)
                    return by[0], _m.last_label()
                out.append(("xkiro", _xk))
        except Exception:
            pass
        try:
            from . import openai_translate
            if openai_translate.key_configured() \
                    and not _probe_blocked("openai"):
                out.append(("openai", lambda seg:
                            (openai_translate.translate_all(
                                [seg], style=self.style)[0],
                             "openai")))
        except Exception:
            pass
        return out

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


def _gemini_attempt(segs, style, report):
    """ONE Gemini attempt with the chain's usual report
    semantics; the translated segment list, or None when the
    engine is unconfigured / probe-skipped / failed (the error
    lands in report["gemini_err"]). Called from the build-189
    lead slot and from Gemini's historical fallback slot — the
    caller guarantees it runs at most once per job."""
    try:
        from . import gemini_translate
        if gemini_translate.key_configured() \
                and not _probe_blocked("gemini"):
            by_idx = gemini_translate.translate_all(segs, style=style)
            out = [{**seg, "text_my": by_idx[i], "provider": "gemini"}
                   for i, seg in enumerate(segs)]
            return _fill_missing(out, report, style=style)
    except Exception as e:
        report["gemini_err"] = str(e)[:120]
    return None


def _openai_attempt(segs, style, report):
    """ONE OpenAI attempt; same contract as _gemini_attempt."""
    try:
        from . import openai_translate
        if openai_translate.key_configured() \
                and not _probe_blocked("openai"):
            by_idx = openai_translate.translate_all(segs, style=style)
            out = [{**seg, "text_my": by_idx[i], "provider": "openai"}
                   for i, seg in enumerate(segs)]
            return _fill_missing(out, report, style=style)
    except Exception as e:
        report["openai_err"] = str(e)[:120]
    return None


def _fill_missing(out, report=None, style="subs"):
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
                rescue = _Rescue(style)
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


def translate_segments(workdir, style="subs"):
    ensure_system_ca()
    workdir = Path(workdir)
    segs = load_segments(workdir / "segments_en.json")
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
    # further down never retries it within the same job.
    gemini_tried = False
    if segs and _probe_engine_ok("gemini"):
        gemini_tried = True
        out = _gemini_attempt(segs, style, report)
        if out is not None:
            _write_report(workdir, report)
            save_segments(out, workdir / "segments_my.json")
            write_srt(out, workdir / "subs_my.srt")
            return out
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
                by_idx = openrouter_translate.translate_all(
                    segs, style=style)
                label = openrouter_translate.last_label()
                out = [{**seg, "text_my": by_idx[i],
                        "provider": label}
                       for i, seg in enumerate(segs)]
                out = _fill_missing(out, report, style=style)
                _write_report(workdir, report)
                save_segments(out, workdir / "segments_my.json")
                write_srt(out, workdir / "subs_my.srt")
                return out
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
                by_idx = xkiro_translate.translate_all(
                    segs, style=style)
                label = xkiro_translate.last_label()
                out = [{**seg, "text_my": by_idx[i],
                        "provider": label}
                       for i, seg in enumerate(segs)]
                out = _fill_missing(out, report, style=style)
                _write_report(workdir, report)
                save_segments(out, workdir / "segments_my.json")
                write_srt(out, workdir / "subs_my.srt")
                return out
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
    # one attempt per job, whichever slot it came from.
    if segs and _probe_blocked("gemini") and not gemini_tried:
        report["gemini_skipped"] = "region-blocked (fresh probe)"
    if segs and not gemini_tried:
        out = _gemini_attempt(segs, style, report)
        if out is not None:
            _write_report(workdir, report)
            save_segments(out, workdir / "segments_my.json")
            write_srt(out, workdir / "subs_my.srt")
            return out
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
    if segs and _probe_blocked("openai"):
        report["openai_skipped"] = "region-blocked (fresh probe)"
    if segs:
        out = _openai_attempt(segs, style, report)
        if out is not None:
            _write_report(workdir, report)
            save_segments(out, workdir / "segments_my.json")
            write_srt(out, workdir / "subs_my.srt")
            return out
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
    rescue = _Rescue(style)
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
    out = _fill_missing(out, report, style=style)
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
        out = _fill_missing(out, tmp, style=style)
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
