"""Video Translator — web server.

Run:  .venv/bin/python app.py        (PORT env, default 8611)
UI:   http://localhost:8611/
API:  POST /api/jobs {url, mode: subs|dub|both, voice}
      GET  /api/jobs/<id>            job state + step list
      GET  /api/jobs/<id>/download   finished .mp4
      GET  /api/voices               available my-MM voices
"""
import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from flask import (Flask, Response, jsonify, request, send_file,
                   send_from_directory)

# CA patch must land before edge_tts/aiohttp are imported anywhere
from pipeline.common import ensure_system_ca

ensure_system_ca()

from pipeline import download as dl_mod
from pipeline import fx as fx_mod
from pipeline import run_job, tts
from pipeline.common import probe_duration, run as sh

ROOT = Path(__file__).resolve().parent
JOBS_DIR = ROOT / "jobs"
JOBS_DIR.mkdir(exist_ok=True)
PREVIEWS_DIR = ROOT / "previews"
PREVIEWS_DIR.mkdir(exist_ok=True)
UPLOADS_DIR = ROOT / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)

app = Flask(__name__, static_folder=None)
JOBS = {}  # id -> dict (also persisted to jobs/<id>/job.json)

STEP_LABELS = {
    "download": "ဗီဒီယို ဒေါင်းလုပ်",
    "transcribe": "စကားသံကို စာသားထုတ်",
    "translate": "မြန်မာ ဘာသာပြန်",
    "tts": "မြန်မာ အသံထုတ်",
    "retime": "အချိန်ကိုက် ညှိ (လိုရင် ဖရိမ်ရပ်)",
    "finalize": "စာတန်းထိုး (ရုပ်ရှင်ပုံစံ) + Logo/Blur + ဗီဒီယို ပေါင်းစပ်",
}


def _save(job):
    p = JOBS_DIR / job["id"] / "job.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")


def _progress_fn(job):
    def progress(step, state, note=""):
        for s in job["steps"]:
            if s["key"] == step:
                s["state"] = state
                if note:
                    s["note"] = note
        job["current"] = step
        _save(job)
    return progress


# ---------- auto-cleanup: no job data stays on the server ----------
# 15 minutes after a job finishes (done or error), its whole folder —
# source, intermediates and output — plus the cached preview copy of
# the same URL is deleted, so nothing accumulates on the disk.
CLEANUP_DELAY = 900
CLEANUP_TIMERS = {}


def _schedule_cleanup(job):
    jid = job["id"]
    old = CLEANUP_TIMERS.pop(jid, None)
    if old:
        old.cancel()
    job["cleanup_at"] = time.time() + CLEANUP_DELAY
    t = threading.Timer(CLEANUP_DELAY, _cleanup_job, args=(jid,))
    t.daemon = True
    t.start()
    CLEANUP_TIMERS[jid] = t


def _cleanup_job(jid):
    CLEANUP_TIMERS.pop(jid, None)
    job = JOBS.pop(jid, None)
    url = (job or {}).get("url")
    if not url:
        try:
            meta = json.loads((JOBS_DIR / jid / "job.json").read_text(encoding="utf-8"))
            url = meta.get("url")
        except Exception:
            url = None
    shutil.rmtree(JOBS_DIR / jid, ignore_errors=True)
    if url:
        _, pdir = _preview_dir(url)
        shutil.rmtree(pdir, ignore_errors=True)


def _fail(job, e):
    job["status"] = "error"
    job["error"] = str(e)[-1500:]
    for s in job["steps"]:
        if s["key"] == job.get("current") and s["state"] == "running":
            s["state"] = "error"
    job["finished_at"] = time.time()
    _schedule_cleanup(job)
    _save(job)


def _run_phase2(job):
    workdir = JOBS_DIR / job["id"]
    run_job.run_phase2(workdir, mode=job["mode"], voice=job["voice"],
                       progress=_progress_fn(job), fx=job.get("fx"),
                       font=job.get("font") or "noto",
                       voice_rate=job.get("voice_rate", 0),
                       voice_pitch=job.get("voice_pitch", 0),
                       subs=job.get("subs"))


def _finish(job):
    job["status"] = "done"
    job["current"] = None
    job["finished_at"] = time.time()
    _schedule_cleanup(job)
    _save(job)


def _worker(job):
    """Full run: download -> transcribe -> translate -> TTS/retime/burn.

    No mandatory review stop: the auto-translated Myanmar text is used
    as-is and the video renders straight through. The user can still
    edit the text afterwards and re-render (see job_review)."""
    workdir = JOBS_DIR / job["id"]
    try:
        job["status"] = "running"
        _save(job)
        run_job.run_phase1(workdir, job["url"], source_path=job.get("source_path"),
                           progress=_progress_fn(job))
        _run_phase2(job)
        _finish(job)
    except Exception as e:  # surface the real error to the UI
        _fail(job, e)


def _worker2(job):
    """Re-render phase 2 from the current (possibly edited) segments."""
    try:
        job["status"] = "running"
        _save(job)
        _run_phase2(job)
        _finish(job)
    except Exception as e:
        _fail(job, e)


@app.get("/")
def index():
    return send_from_directory(ROOT / "static", "index.html")


@app.get("/static/<path:filename>")
def static_files(filename):
    # static_folder is disabled above; serve the PWA manifest/icons explicitly.
    return send_from_directory(ROOT / "static", filename)


@app.get("/sw.js")
def service_worker():
    # Served from the root so its scope covers the whole app.
    return send_from_directory(ROOT / "static", "sw.js")


@app.get("/api/voices")
def voices():
    try:
        import asyncio
        import edge_tts
        proxy = (os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
                 or os.environ.get("http_proxy") or None)
        vs = asyncio.run(edge_tts.list_voices(proxy=proxy))
        mine = [{"id": v["ShortName"],
                 "label": ("အမျိုးသမီး (Nilar)" if "Nilar" in v["ShortName"] else "အမျိုးသား (Thiha)")}
                for v in vs if v["Locale"] == "my-MM"]
        if mine:
            return jsonify(mine)
    except Exception:
        pass
    return jsonify([{"id": v, "label": v} for v in tts.FALLBACK_VOICES])


def _preview_dir(url):
    pid = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return pid, PREVIEWS_DIR / pid


@app.post("/api/preview")
def make_preview():
    """Download (once, cached by URL) and report size/duration for the editor."""
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "လင့်ခ် မမှန်ပါ"}), 400
    pid, pdir = _preview_dir(url)
    pdir.mkdir(parents=True, exist_ok=True)
    src = pdir / "source.mp4"
    try:
        if not src.exists():
            dl_mod.download(url, pdir)
        dur = probe_duration(src)
        w, h = fx_mod.video_size(src)
    except Exception as e:
        return jsonify({"error": str(e)[-500:]}), 502
    title = None
    info_p = pdir / "source_info.json"
    if info_p.exists():
        try:
            title = json.loads(info_p.read_text(encoding="utf-8")).get("title")
        except Exception:
            pass
    return jsonify({"preview_id": pid, "duration": round(dur, 2),
                    "width": w, "height": h, "title": title})


@app.get("/api/preview/<pid>/frame")
def preview_frame(pid):
    src = PREVIEWS_DIR / pid / "source.mp4"
    if not src.exists():
        return jsonify({"error": "preview မတွေ့ပါ"}), 404
    try:
        t = float(request.args.get("t", "2"))
    except ValueError:
        t = 2.0
    dur = probe_duration(src)
    t = min(max(t, 0.0), max(dur - 0.15, 0.0))
    frame = PREVIEWS_DIR / pid / f"frame_{t:.1f}.jpg"
    if not frame.exists():
        sh(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(src),
            "-frames:v", "1", "-q:v", "3", str(frame)])
    return send_file(frame, mimetype="image/jpeg")


@app.post("/api/logo")
def upload_logo():
    f = request.files.get("logo")
    if not f or not f.filename:
        return jsonify({"error": "logo ဖိုင် မပါပါ"}), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp"):
        return jsonify({"error": "PNG/JPG/WebP ပဲ ရပါတယ်"}), 400
    lid = uuid.uuid4().hex[:12]
    f.save(UPLOADS_DIR / f"{lid}{ext}")
    return jsonify({"logo_id": lid})


def _build_fx(data):
    """Normalize the editor payload into the fx dict run_job/pipeline.fx eats."""
    fx_in = data.get("fx") or {}
    fx = {}
    blur = fx_in.get("blur")
    if blur:
        fx["blur"] = {"x": float(blur["x"]), "y": float(blur["y"]),
                      "w": float(blur["w"]), "h": float(blur["h"]),
                      "strength": float(blur.get("strength") or 20)}
    logo_in = fx_in.get("logo") or {}
    logo_id = logo_in.get("logo_id")
    if logo_id:
        matches = list(UPLOADS_DIR.glob(f"{logo_id}.*"))
        if matches:
            fx["logo"] = {"path": str(matches[0]),
                          "pos_x": float(logo_in.get("pos_x", 1.0)),
                          "pos_y": float(logo_in.get("pos_y", 0.0)),
                          "size_pct": float(logo_in.get("size_pct", 18)),
                          "opacity": float(logo_in.get("opacity", 70)),
                          "saturation": float(logo_in.get("saturation", 100))}
    return fx


FONT_LABELS = {
    "noto": "Noto Sans Myanmar — ထူ (ရုပ်ရှင်ပုံစံ မူလ)",
    "pyidaungsu": "ပြည်ထောင်စု (Pyidaungsu)",
    "padauk": "ပိတောက် (Padauk Bold)",
}


@app.get("/api/fonts")
def fonts():
    from pipeline import burn as burn_mod
    return jsonify([{"id": k, "label": FONT_LABELS.get(k, k)}
                    for k in burn_mod.FONT_CHOICES])


# ---------- Gemini key (server-side only) ----------
# The user pastes the key into THIS page (behind the app's login) — it
# must never travel through chat. It is stored in the project .env
# (chmod 600, survives app updates, never in git) and no endpoint ever
# returns the value, only whether one is configured.

@app.get("/api/settings/gemini-key")
def gemini_key_status():
    from pipeline import gemini_translate
    return jsonify({"configured": gemini_translate.key_configured()})


@app.post("/api/settings/gemini-key")
def gemini_key_save():
    data = request.get_json(force=True) or {}
    key = (data.get("key") or "").strip()
    if not (key.startswith("AIza") and 20 <= len(key) <= 80):
        return jsonify({"error": "key မမှန်ပါ — AIza နဲ့ စတဲ့ Gemini key ဖြစ်ရပါမယ်"}), 400
    env_path = ROOT / ".env"
    lines = []
    if env_path.exists():
        lines = [ln for ln in env_path.read_text(encoding="utf-8").splitlines()
                 if not ln.startswith("GEMINI_API_KEY=")]
    lines.append(f"GEMINI_API_KEY={key}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(env_path, 0o600)
    except Exception:
        pass
    return jsonify({"ok": True, "configured": True})


@app.delete("/api/settings/gemini-key")
def gemini_key_delete():
    env_path = ROOT / ".env"
    if env_path.exists():
        lines = [ln for ln in env_path.read_text(encoding="utf-8").splitlines()
                 if not ln.startswith("GEMINI_API_KEY=")]
        env_path.write_text("\n".join(lines) + ("\n" if lines else ""),
                            encoding="utf-8")
    return jsonify({"ok": True, "configured": False})


def _clamp_num(v, lo, hi, default):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _build_subs(data):
    """Subtitle layout params, clamped server-side; absent = previous look
    (burn.SUBS_DEFAULTS: size 100, width 95, bottom 10, bottom-aligned)."""
    s = data.get("subs") or {}
    return {
        "size_pct": _clamp_num(s.get("size_pct"), 60, 160, 100),
        "width_pct": _clamp_num(s.get("width_pct"), 50, 100, 95),
        "bottom_pct": _clamp_num(s.get("bottom_pct"), 2, 25, 10),
        "align": "top" if s.get("align") == "top" else "bottom",
    }


@app.post("/api/jobs")
def create_job():
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    mode = data.get("mode") or "both"
    voice = data.get("voice") or tts.DEFAULT_VOICE
    font = data.get("font") or "noto"
    subs = _build_subs(data)
    # Voice tuning: percent speed (-50..+100) and Hz pitch (-50..+50);
    # clamped again in pipeline.tts; defaults 0 keep prior behaviour.
    try:
        voice_rate = max(-50, min(100, int(data.get("voice_rate") or 0)))
    except (TypeError, ValueError):
        voice_rate = 0
    try:
        voice_pitch = max(-50, min(50, int(data.get("voice_pitch") or 0)))
    except (TypeError, ValueError):
        voice_pitch = 0
    if font not in FONT_LABELS:
        return jsonify({"error": "font မမှန်ပါ"}), 400
    if not url.startswith(("http://", "https://")):
        return jsonify({"error": "လင့်ခ် မမှန်ပါ"}), 400
    if mode not in ("subs", "dub", "both"):
        return jsonify({"error": "mode မမှန်ပါ"}), 400
    source_path = None
    preview_id = data.get("preview_id")
    if preview_id:
        cand = PREVIEWS_DIR / str(preview_id) / "source.mp4"
        if cand.exists():
            source_path = str(cand)
    jid = uuid.uuid4().hex[:12]
    job = {
        "id": jid, "url": url, "mode": mode, "voice": voice, "font": font,
        "subs": subs,
        "voice_rate": voice_rate, "voice_pitch": voice_pitch,
        "fx": _build_fx(data), "source_path": source_path,
        "status": "queued", "current": None, "error": None,
        "created_at": time.time(), "finished_at": None,
        "steps": [{"key": k, "label": STEP_LABELS[k], "state": "pending"}
                  for k in run_job.steps_for_mode(mode)],
    }
    JOBS[jid] = job
    _save(job)
    threading.Thread(target=_worker, args=(job,), daemon=True).start()
    return jsonify(job)


@app.get("/api/jobs/<jid>")
def get_job(jid):
    job = _load_job(jid)
    if not job:
        return jsonify({"error": "job မတွေ့ပါ"}), 404
    return jsonify(job)


def _load_job(jid):
    job = JOBS.get(jid)
    if not job:
        p = JOBS_DIR / jid / "job.json"
        if not p.exists():
            return None
        job = json.loads(p.read_text(encoding="utf-8"))
        JOBS[jid] = job
    return job


@app.get("/api/jobs/<jid>/segments")
def job_segments(jid):
    """Segments for the review/edit step (after phase 1)."""
    job = _load_job(jid)
    if not job:
        return jsonify({"error": "job မတွေ့ပါ"}), 404
    seg_path = JOBS_DIR / jid / "segments_my.json"
    if not seg_path.exists():
        return jsonify({"error": "အပိုင်းတွေ မရသေးပါ"}), 409
    from pipeline.common import load_segments
    segs = load_segments(seg_path)
    return jsonify({"status": job["status"], "segments": [
        {"idx": i, "start": s.get("start"), "end": s.get("end"),
         "text": s.get("text"), "text_my": s.get("text_my"),
         "provider": s.get("provider"), "edited": bool(s.get("edited"))}
        for i, s in enumerate(segs)]})


@app.post("/api/jobs/<jid>/review")
def job_review(jid):
    """Submit edits (or none) and (re-)run phase 2.

    Body: {"edits": [{"idx": 0, "text_my": "..."}, ...]} — segments not
    listed keep their translated text. Allowed on a finished job (the
    normal flow renders straight through; this is the optional
    edit-and-rerun path) and on a legacy waiting_review job.
    """
    job = _load_job(jid)
    if not job:
        return jsonify({"error": "job မတွေ့ပါ"}), 404
    if job["status"] not in ("waiting_review", "done"):
        return jsonify({"error": "ဒီ job က ပြင်ဆင်ရန် အဆင့် မဟုတ်ပါ"}), 409
    data = request.get_json(force=True) or {}
    edits = {int(e["idx"]): str(e["text_my"]) for e in data.get("edits", [])
             if "idx" in e and "text_my" in e}
    from pipeline.common import load_segments, save_segments, write_srt
    seg_path = JOBS_DIR / jid / "segments_my.json"
    segs = load_segments(seg_path)
    changed = 0
    for idx, new_text in edits.items():
        if 0 <= idx < len(segs) and new_text.strip() \
                and new_text.strip() != (segs[idx].get("text_my") or "").strip():
            segs[idx]["text_my"] = new_text.strip()
            segs[idx]["edited"] = True
            changed += 1
    save_segments(segs, seg_path)
    write_srt(segs, JOBS_DIR / jid / "subs_my.srt")  # keep subs-mode SRT in sync
    job["edited_count"] = changed
    # A re-render supersedes the pending auto-cleanup of the old result.
    old_timer = CLEANUP_TIMERS.pop(jid, None)
    if old_timer:
        old_timer.cancel()
    job.pop("cleanup_at", None)
    _save(job)
    threading.Thread(target=_worker2, args=(job,), daemon=True).start()
    return jsonify({"ok": True, "edited_count": changed})


@app.get("/api/jobs/<jid>/download")
def download_job(jid):
    out = JOBS_DIR / jid / "output.mp4"
    if not out.exists():
        return jsonify({"error": "မပြီးသေးပါ"}), 404
    return send_file(out, as_attachment=True, download_name=f"myanmar-{jid}.mp4")


@app.get("/api/jobs/<jid>/stream")
def stream_job(jid):
    """Inline playback for the result preview (the download route's
    attachment header prevents some phone browsers from playing it)."""
    out = JOBS_DIR / jid / "output.mp4"
    if not out.exists():
        return jsonify({"error": "မပြီးသေးပါ"}), 404
    return send_file(out, as_attachment=False)


@app.delete("/api/jobs/<jid>")
def delete_job(jid):
    """Delete a job and all its files right now (the user asked for no
    leftovers; otherwise the 15-minute auto-cleanup does it)."""
    job = _load_job(jid)
    if not job:
        return jsonify({"error": "job မတွေ့ပါ"}), 404
    if job["status"] in ("queued", "running"):
        return jsonify({"error": "လုပ်နေဆဲ job ကို မဖျက်နိုင်ပါ"}), 409
    old_timer = CLEANUP_TIMERS.pop(jid, None)
    if old_timer:
        old_timer.cancel()
    _cleanup_job(jid)
    return jsonify({"ok": True})


# ---------- text-only export + bulk text input ----------
# Both read/write the SAME store the review API uses (segments_my.json),
# so exports always reflect the user's saved edits, and bulk-pasted text
# is exactly what phase 2 dubs (TTS) and burns (subtitles) in mode "both".

def _segments_or_error(jid):
    """(job, segments, error_response) — segments None when error is set."""
    job = _load_job(jid)
    if not job:
        return None, None, (jsonify({"error": "job မတွေ့ပါ"}), 404)
    seg_path = JOBS_DIR / jid / "segments_my.json"
    if not seg_path.exists():
        return job, None, (jsonify({"error": "စာသားတွေ မရသေးပါ"}), 404)
    from pipeline.common import load_segments
    return job, load_segments(seg_path), None


def _export_lang():
    """(text_key, lang) or None for a bad ?lang= value."""
    lang = request.args.get("lang", "my")
    if lang not in ("my", "src"):
        return None
    return ("text_my" if lang == "my" else "text"), lang


@app.get("/api/jobs/<jid>/export/txt")
def export_txt(jid):
    """Plain text of a job's segments — one segment per paragraph.
    Default: edited Myanmar (text_my); ?lang=src for the source text.
    Works as soon as segments exist; no video render needed."""
    job, segs, err = _segments_or_error(jid)
    if err:
        return err
    got = _export_lang()
    if not got:
        return jsonify({"error": "lang မမှန်ပါ"}), 400
    key, lang = got
    paras = [(s.get(key) or "").strip() for s in segs]
    body = "\n\n".join(p for p in paras if p) + "\n"
    return Response(body, mimetype="text/plain; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{jid}_{lang}.txt"'})


@app.get("/api/jobs/<jid>/export/srt")
def export_srt(jid):
    """Proper SRT from the segments with their timings (sequential indices,
    segments with empty text are skipped and numbering stays sequential)."""
    job, segs, err = _segments_or_error(jid)
    if err:
        return err
    got = _export_lang()
    if not got:
        return jsonify({"error": "lang မမှန်ပါ"}), 400
    key, lang = got
    from pipeline.common import fmt_ts
    lines, n = [], 0
    for s in segs:
        text = (s.get(key) or "").strip()
        if not text:
            continue
        n += 1
        lines += [str(n), f"{fmt_ts(s['start'])} --> {fmt_ts(s['end'])}",
                  text, ""]
    return Response("\n".join(lines), mimetype="text/plain; charset=utf-8",
                    headers={"Content-Disposition":
                             f'attachment; filename="{jid}_{lang}.srt"'})


def _split_bulk_units(text, n_segments):
    """Split pasted text into units to distribute over segments.

    Rule: (1) split by line breaks (non-empty lines); if that count does
    not equal the segment count, (2) re-split the whole text into
    sentences on the enders ။ and '.' and use those instead. Either list
    is then assigned sequentially (see job_review_bulk for over/underflow).
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) == n_segments:
        return lines
    flat = " ".join(lines)
    sents = [p.strip() for p in re.split(r"(?<=[။.])\s*", flat) if p.strip()]
    return sents or lines


@app.post("/api/jobs/<jid>/review/bulk")
def job_review_bulk(jid):
    """Paste a whole translated text at once; it is distributed over the
    segments and saved as review edits (edited=True) WITHOUT starting
    phase 2 — the user can still tweak per-segment boxes, then continue
    with the normal review submit.

    Distribution: units (lines, or sentences — see _split_bulk_units) are
    assigned to segments in order. Extra units beyond the segment count
    are joined into the LAST segment; if there are fewer units than
    segments, the remaining segments keep their current text.
    """
    job = _load_job(jid)
    if not job:
        return jsonify({"error": "job မတွေ့ပါ"}), 404
    if job["status"] not in ("waiting_review", "done"):
        return jsonify({"error": "ဒီ job က ပြင်ဆင်ရန် အဆင့် မဟုတ်ပါ"}), 409
    data = request.get_json(force=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "စာသား ထည့်ပါ"}), 400
    job, segs, err = _segments_or_error(jid)
    if err:
        return err
    if not segs:
        return jsonify({"error": "အပိုင်းတွေ မရှိပါ"}), 409
    units = _split_bulk_units(text, len(segs))
    assign = {}
    if len(units) >= len(segs):
        for i in range(len(segs) - 1):
            assign[i] = units[i]
        assign[len(segs) - 1] = " ".join(units[len(segs) - 1:])
    else:
        for i, u in enumerate(units):
            assign[i] = u
    from pipeline.common import save_segments, write_srt
    changed = 0
    for i, new_text in assign.items():
        if new_text and new_text != (segs[i].get("text_my") or ""):
            segs[i]["text_my"] = new_text
            segs[i]["edited"] = True
            changed += 1
    seg_path = JOBS_DIR / jid / "segments_my.json"
    save_segments(segs, seg_path)
    write_srt(segs, JOBS_DIR / jid / "subs_my.srt")  # keep SRT in sync
    return jsonify({"ok": True, "changed": changed, "segments": [
        {"idx": i, "text_my": s.get("text_my")} for i, s in enumerate(segs)]})


def _startup_sweep():
    """Remove leftovers from before a restart: job folders and preview
    caches older than an hour are deleted (nothing is kept long-term)."""
    now = time.time()
    for d in list(JOBS_DIR.iterdir()):
        if not d.is_dir():
            continue
        ref, url = None, None
        try:
            meta = json.loads((d / "job.json").read_text(encoding="utf-8"))
            ref = meta.get("finished_at") or meta.get("created_at")
            url = meta.get("url")
        except Exception:
            pass
        if ref and now - float(ref) > 3600:
            shutil.rmtree(d, ignore_errors=True)
            if url:
                _, pdir = _preview_dir(url)
                shutil.rmtree(pdir, ignore_errors=True)
    for d in list(PREVIEWS_DIR.iterdir()):
        try:
            if d.is_dir() and now - d.stat().st_mtime > 3600:
                shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


try:
    _startup_sweep()
except Exception:
    pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8611"))
    app.run(host="0.0.0.0", port=port, threaded=True)
