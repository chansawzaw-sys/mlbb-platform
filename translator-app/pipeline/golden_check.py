"""Golden Sample quality regression (build 230, owner request).

A FIXED set of short reference clips (golden/golden_set.json) whose
known-good pipeline outputs are stored as baselines
(golden/baselines/<id>.json). Re-running transcribe -> translate
(plus one TTS sample) on the same clips and comparing against the
baselines turns "quality" from a feeling into a measurement: when
a free translation engine is silently swapped or degraded by its
provider, a TTS model changes, or a prompt edit regresses output,
the deltas show up here BEFORE a customer hits them.

Checks per clip (all objective, no LLM judge):
  - untranslated-segment ratio, via translate_segments'
    translation_stats (the same _seg_untranslated metric behind
    run_phase1's majority gate — a run that would trip that gate
    in production FAILs here);
  - segment-count and text-length drift (source + Myanmar);
  - the engine/model that actually served translation (the
    dominant per-segment `provider` label) — a change is a WARN
    naming old -> new, never silent;
  - for the TTS clip: synthesis success, per-segment providers,
    and total spoken-duration drift.

Verdicts: PASS / WARN / FAIL per check; a clip's verdict is its
worst check; the run's overall verdict is its worst clip.

Storage: everything lives under golden/ in the project root —
OUTSIDE jobs/, so the build-215 24h job-data cleanup (which only
ever removes jobs/<id> dirs) can never reap the set or its
baselines. Clips ship as .mp4.b64 text (the deploy channel is
text-only, the same constraint as the fonts — see
common.ensure_fonts) and are materialized to .mp4 on first use.

Admin surface (app.py): GET/POST /api/admin/golden-check and
POST /api/admin/golden-check/capture, admin-only like the
api-stats board. CLI: `python -m pipeline.golden_check` runs a
check, `--capture` (re)writes the baselines from CURRENT code.

Build 276: translation runs through WHATEVER the Admin
translate-model choice booted with (this module never named
an engine — _run_pipeline calls translate_segments, whose
OpenAI-only walk follows the choice), so the same samples
can be run against GPT-6 Luna and compared with the
gpt-4o-mini baseline. Every run/capture is stamped with the
model that headed the walk (report["translate_model"], each
baseline's "translate_model", and the formatted header) so
a Luna run is never mistaken for a 4o-mini run.
Baselines are environment-specific: the engines that serve a
local run (free chain) differ from the server's (Gemini /
OpenRouter lead), so after deploying, re-capture once on the
server so its baselines reflect production engines.
"""
import base64
import json
import shutil
import sys
import threading
import time
from collections import Counter
from pathlib import Path

from .common import PROJECT_ROOT

GOLDEN_DIR = PROJECT_ROOT / "golden"
CLIPS_DIR = GOLDEN_DIR / "clips"
BASELINES_DIR = GOLDEN_DIR / "baselines"
REPORTS_DIR = GOLDEN_DIR / "reports"
WORK_DIR = GOLDEN_DIR / "work"
CONFIG_PATH = GOLDEN_DIR / "golden_set.json"

# The TTS sample uses the standard male dub voice (Thiha) — the
# voice production dub jobs default to in practice.
TTS_VOICE = "my-MM-ThihaNeural"

# Drift thresholds (fractions). Segment counts and Whisper text
# length wobble a little run to run even with no real change;
# translation length wobbles more (engines rephrase). The
# untranslated ratio has its own rule in compare() below.
SEG_COUNT_WARN, SEG_COUNT_FAIL = 0.15, 0.35
SRC_CHARS_WARN, SRC_CHARS_FAIL = 0.15, 0.35
MY_CHARS_WARN, MY_CHARS_FAIL = 0.20, 0.40
TTS_DUR_WARN, TTS_DUR_FAIL = 0.20, 0.40
UNTRANSLATED_WARN_ABS = 0.34     # ratio above this warns on its own
UNTRANSLATED_WARN_RISE = 0.15    # ...or rising this far over baseline

_RANK = {"PASS": 0, "WARN": 1, "FAIL": 2}


def _worst(*verdicts):
    return max(verdicts, key=lambda v: _RANK[v])


def load_config():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _translate_model_label():
    """Build 276: the model heading the translation walk
    for THIS process (the Admin translate-model choice) —
    stamped on every run/capture so a Golden Sample result
    says which model produced it. Build 279: under the
    "gemini" choice the walk runs on Gemini, so the label
    names the engine and its head model —
    "gemini:<model>"; the OpenAI choices keep their
    build-276 label (the walk's head model)."""
    try:
        from . import openai_translate
        if openai_translate.model_choice() == "gemini":
            from . import gemini_translate
            models = gemini_translate._models()
            return "gemini:" + (models[0] if models
                                else "gemini")
        return openai_translate.translate_models()[0]
    except Exception:
        return None


def materialize_clips():
    """Decode any clips/<name>.mp4.b64 whose .mp4 is missing or
    stale-sized — the ensure_fonts pattern for binary assets on
    a text-only deploy channel."""
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    for b64 in sorted(CLIPS_DIR.glob("*.mp4.b64")):
        mp4 = b64.with_suffix("")
        try:
            raw = base64.b64decode(b64.read_text(encoding="ascii"))
        except Exception:
            continue
        if not mp4.exists() or mp4.stat().st_size != len(raw):
            mp4.write_bytes(raw)
    return CLIPS_DIR


def _clip_path(clip):
    materialize_clips()
    p = CLIPS_DIR / clip["file"]
    if not p.exists():
        raise RuntimeError(f"golden clip missing: {clip['file']}")
    return p


def _metrics(segs_en, segs_my, tts_info=None):
    from .translate_segments import translation_stats
    stats = translation_stats(segs_my or [])
    provs = Counter((s.get("provider") or "?") for s in (segs_my or [])
                    if (s.get("text") or "").strip())
    m = {
        "seg_count": len(segs_en or []),
        "src_chars": sum(len((s.get("text") or "").strip())
                         for s in (segs_en or [])),
        "my_chars": sum(len((s.get("text_my") or "").strip())
                        for s in (segs_my or [])),
        "total": stats["total"],
        "untranslated": stats["untranslated"],
        "untranslated_ratio": round(
            stats["untranslated"] / stats["total"], 3)
        if stats["total"] else 0.0,
        "providers": dict(provs),
        "dominant_provider": provs.most_common(1)[0][0] if provs else None,
    }
    if tts_info is not None:
        m["tts"] = tts_info
    return m


def _run_pipeline(clip, workdir):
    """Run the REAL pipeline stages on one golden clip in a
    scratch workdir and return (metrics, segs_en, segs_my).
    Raises on stage failure — the caller records it as a FAIL,
    exactly like a production job failing loudly."""
    from . import transcribe, translate_segments, tts
    workdir = Path(workdir)
    if workdir.exists():
        shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir(parents=True, exist_ok=True)
    src = workdir / "source.mp4"
    shutil.copyfile(_clip_path(clip), src)
    transcribe.transcribe(src, workdir, use_captions=True)
    segs_en = json.loads((workdir / "segments_en.json")
                         .read_text(encoding="utf-8"))
    segs_my = translate_segments.translate_segments(workdir,
                                                    style="subs")
    tts_info = None
    if clip.get("tts"):
        out = tts.synthesize(workdir, voice=TTS_VOICE)
        spoken = [s for s in out if s.get("tts_file")]
        tts_info = {
            "voice": TTS_VOICE,
            "segments": len(spoken),
            "total_duration_s": round(
                sum(float(s.get("tts_duration") or 0) for s in spoken), 2),
            "providers": dict(Counter(
                s.get("tts_provider") or "?" for s in spoken)),
            "failed_silent": sum(
                1 for s in spoken
                if (s.get("tts_provider") or "") == "FAILED-silent"),
        }
    return _metrics(segs_en, segs_my, tts_info), segs_en, segs_my


def capture_baselines(log=print):
    """(Re)write golden/baselines/<id>.json from the CURRENT
    code and CURRENT engines. Run once per environment (local /
    server) and after any intentional engine/prompt change."""
    BASELINES_DIR.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    label = _translate_model_label()
    done = []
    for clip in cfg["clips"]:
        log(f"capture {clip['id']} …")
        metrics, segs_en, segs_my = _run_pipeline(
            clip, WORK_DIR / ("cap-" + clip["id"]))
        base = {
            "id": clip["id"], "genre": clip["genre"],
            "lang": clip["lang"], "file": clip["file"],
            "captured_at": int(time.time()),
            "translate_model": label,
            "metrics": metrics,
            "segments_en": segs_en, "segments_my": segs_my,
        }
        tmp = BASELINES_DIR / (clip["id"] + ".json.tmp")
        tmp.write_text(json.dumps(base, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(BASELINES_DIR / (clip["id"] + ".json"))
        done.append(clip["id"])
        log(f"capture {clip['id']}: {metrics['seg_count']} segs, "
            f"untranslated {metrics['untranslated']}/{metrics['total']}, "
            f"via {metrics['dominant_provider']}")
    return done


def _drift(name, base_v, now_v, warn, fail, checks):
    if not base_v:
        return
    d = abs(now_v - base_v) / base_v
    verdict = "PASS" if d <= warn else "WARN" if d <= fail else "FAIL"
    checks.append({
        "name": name, "verdict": verdict,
        "detail": f"{base_v} -> {now_v} ({d * 100:.0f}% drift)"})


def compare(base_metrics, now_metrics):
    """Objective baseline comparison. Returns (verdict, checks)."""
    checks = []
    b, n = base_metrics, now_metrics
    # Untranslated ratio — the headline quality signal. FAIL when
    # the production majority gate (run_phase1) would trip, or
    # half the clip stayed untranslated; WARN on a material rise.
    total, un = n["total"], n["untranslated"]
    if total >= 4 and un * 2 > total:
        checks.append({"name": "untranslated", "verdict": "FAIL",
                       "detail": f"{un}/{total} untranslated — the "
                                 "production majority gate would fail "
                                 "this job"})
    elif n["untranslated_ratio"] > UNTRANSLATED_WARN_ABS or \
            n["untranslated_ratio"] - b["untranslated_ratio"] \
            > UNTRANSLATED_WARN_RISE:
        checks.append({"name": "untranslated", "verdict": "WARN",
                       "detail": f"ratio {b['untranslated_ratio']} -> "
                                 f"{n['untranslated_ratio']} "
                                 f"({un}/{total})"})
    else:
        checks.append({"name": "untranslated", "verdict": "PASS",
                       "detail": f"{un}/{total} untranslated "
                                 f"(baseline {b['untranslated']}/"
                                 f"{b['total']})"})
    _drift("seg_count", b["seg_count"], n["seg_count"],
           SEG_COUNT_WARN, SEG_COUNT_FAIL, checks)
    _drift("src_chars", b["src_chars"], n["src_chars"],
           SRC_CHARS_WARN, SRC_CHARS_FAIL, checks)
    _drift("my_chars", b["my_chars"], n["my_chars"],
           MY_CHARS_WARN, MY_CHARS_FAIL, checks)
    # Engine/model that served translation — a change is exactly
    # the silent swap this system exists to surface.
    if b["dominant_provider"] != n["dominant_provider"]:
        checks.append({
            "name": "engine", "verdict": "WARN",
            "detail": f"dominant provider {b['dominant_provider']} "
                      f"-> {n['dominant_provider']}"})
    else:
        checks.append({"name": "engine", "verdict": "PASS",
                       "detail": f"served by {n['dominant_provider']}"})
    # TTS sample (only on the clip that has one).
    if "tts" in b or "tts" in n:
        bt, nt = b.get("tts"), n.get("tts")
        if not nt:
            checks.append({"name": "tts", "verdict": "FAIL",
                           "detail": "TTS sample missing this run"})
        elif nt.get("failed_silent"):
            checks.append({"name": "tts", "verdict": "WARN",
                           "detail": f"{nt['failed_silent']} segment(s) "
                                     "FAILED-silent; providers "
                                     f"{nt['providers']}"})
        elif bt:
            _drift("tts_duration_s", bt["total_duration_s"],
                   nt["total_duration_s"], TTS_DUR_WARN, TTS_DUR_FAIL,
                   checks)
            checks.append({"name": "tts", "verdict": "PASS",
                           "detail": f"{nt['segments']} segs, "
                                     f"{nt['total_duration_s']}s via "
                                     f"{nt['providers']}"})
    verdict = "PASS"
    for c in checks:
        verdict = _worst(verdict, c["verdict"])
    return verdict, checks


def run_check(log=print):
    """Full regression run. Returns the report dict; also writes
    golden/reports/latest.json + latest.txt (atomic, like
    api_stats). A clip whose pipeline raises is a FAIL with the
    error recorded — never a crashed run."""
    cfg = load_config()
    label = _translate_model_label()
    report = {"at": int(time.time()), "overall": "PASS",
              "translate_model": label, "clips": []}
    for clip in cfg["clips"]:
        entry = {"id": clip["id"], "genre": clip["genre"],
                 "lang": clip["lang"], "translate_model": label}
        base_path = BASELINES_DIR / (clip["id"] + ".json")
        if not base_path.exists():
            entry.update(verdict="FAIL", checks=[{
                "name": "baseline", "verdict": "FAIL",
                "detail": "no baseline captured yet — run capture "
                          "first"}])
            report["clips"].append(entry)
            report["overall"] = _worst(report["overall"], "FAIL")
            continue
        base = json.loads(base_path.read_text(encoding="utf-8"))
        try:
            now_metrics, _, _ = _run_pipeline(
                clip, WORK_DIR / ("chk-" + clip["id"]))
        except Exception as e:
            entry.update(verdict="FAIL", checks=[{
                "name": "pipeline", "verdict": "FAIL",
                "detail": f"pipeline raised: {str(e)[:200]}"}])
            report["clips"].append(entry)
            report["overall"] = _worst(report["overall"], "FAIL")
            log(f"check {clip['id']}: FAIL (pipeline raised)")
            continue
        verdict, checks = compare(base["metrics"], now_metrics)
        entry.update(verdict=verdict, checks=checks,
                     metrics=now_metrics,
                     baseline_at=base["captured_at"])
        report["clips"].append(entry)
        report["overall"] = _worst(report["overall"], verdict)
        log(f"check {clip['id']}: {verdict}")
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = REPORTS_DIR / "latest.json.tmp"
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(REPORTS_DIR / "latest.json")
    (REPORTS_DIR / "latest.txt").write_text(format_report(report),
                                            encoding="utf-8")
    return report


def format_report(report):
    head = (f"Golden Sample check — overall: {report['overall']} "
            f"(at {time.strftime('%Y-%m-%d %H:%M', time.localtime(report['at']))})")
    if report.get("translate_model"):
        head += f" · translation model: {report['translate_model']}"
    lines = [head]
    for c in report["clips"]:
        lines.append(f"[{c['verdict']}] {c['id']} "
                     f"({c['genre']}, {c['lang']})")
        for chk in c["checks"]:
            mark = "" if chk["verdict"] == "PASS" else "  <<"
            lines.append(f"  {chk['verdict']:4} {chk['name']}: "
                         f"{chk['detail']}{mark}")
    return "\n".join(lines)


# ---------- background-run state for the Admin trigger ----------
_STATE = {"running": False, "kind": None, "started": None,
          "error": None}
_LOCK = threading.Lock()


def status():
    report = None
    p = REPORTS_DIR / "latest.json"
    if p.exists():
        try:
            report = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            report = None
    with _LOCK:
        return {"running": _STATE["running"], "kind": _STATE["kind"],
                "started": _STATE["started"], "error": _STATE["error"],
                "baselines": sorted(
                    f.stem for f in BASELINES_DIR.glob("*.json"))
                if BASELINES_DIR.exists() else [],
                "report": report}


def start(kind):
    """Start a check or capture in a daemon thread (a full run
    takes minutes — Whisper + translation — so the Admin request
    must not block). Returns False when one is already running."""
    with _LOCK:
        if _STATE["running"]:
            return False
        _STATE.update(running=True, kind=kind,
                      started=int(time.time()), error=None)

    def _work():
        try:
            if kind == "capture":
                capture_baselines(log=lambda *a: None)
            else:
                run_check(log=lambda *a: None)
        except Exception as e:  # surfaced via status(), never lost
            with _LOCK:
                _STATE["error"] = str(e)[:300]
        finally:
            with _LOCK:
                _STATE["running"] = False

    threading.Thread(target=_work, daemon=True).start()
    return True


if __name__ == "__main__":
    if "--capture" in sys.argv:
        capture_baselines()
        print("baselines captured")
    else:
        rep = run_check()
        print(format_report(rep))
        sys.exit({"PASS": 0, "WARN": 1, "FAIL": 2}[rep["overall"]])
