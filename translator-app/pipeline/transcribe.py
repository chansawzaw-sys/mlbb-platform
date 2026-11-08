"""Step 2: transcribe speech to timestamped segments.

Default: faster-whisper (model size from WHISPER_MODEL env, default "small", CPU int8).
Fast path: --captions uses a .vtt caption file already fetched by download.py.

CLI: python -m pipeline.transcribe <source.mp4> <workdir> [--captions]
Writes <workdir>/segments_en.json  [{start, end, text}]
"""
import os
import re
import sys
from pathlib import Path

from .common import run, save_segments

MIN_DUR = 0.4  # drop ultra-short noise segments


def _parse_vtt(vtt_path: Path):
    text = Path(vtt_path).read_text(encoding="utf-8", errors="ignore")
    segs, cur = [], None
    ts_re = re.compile(r"(\d+):(\d+):(\d+)[.,](\d+)\s+-->\s+(\d+):(\d+):(\d+)[.,](\d+)")

    def to_sec(m, a, b, c, d):
        return int(m[a]) * 3600 + int(m[b]) * 60 + int(m[c]) + int(m[d]) / 1000.0

    for line in text.splitlines():
        m = ts_re.search(line)
        if m:
            if cur and cur["text"].strip():
                segs.append(cur)
            cur = {"start": to_sec(m, 1, 2, 3, 4), "end": to_sec(m, 5, 6, 7, 8), "text": ""}
        elif cur is not None and line.strip() and not line.startswith(("WEBVTT", "Kind:", "Language:")):
            clean = re.sub(r"<[^>]+>", "", line).strip()
            if clean and clean not in cur["text"]:
                cur["text"] = (cur["text"] + " " + clean).strip()
    if cur and cur["text"].strip():
        segs.append(cur)
    # merge duplicates from rolling auto-captions: keep ones with unique start
    out, seen = [], set()
    for s in segs:
        key = round(s["start"], 1)
        if key in seen or s["end"] - s["start"] < 0.2:
            continue
        seen.add(key)
        out.append(s)
    return out


def transcribe(video_path, workdir, use_captions=False):
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    if use_captions:
        vtts = sorted(workdir.glob("source.*.vtt"))
        if vtts:
            segs = _parse_vtt(vtts[0])
            if segs:
                save_segments(segs, workdir / "segments_en.json")
                return segs
        # fall through to whisper if captions unusable

    # extract 16k mono wav for whisper
    wav = workdir / "audio16k.wav"
    run(["ffmpeg", "-y", "-v", "error", "-i", str(video_path),
         "-vn", "-ac", "1", "-ar", "16000", str(wav)])

    from faster_whisper import WhisperModel
    # Prefer the project-local model (models/faster-whisper-small) when present
    # so the app is self-contained and never re-downloads; else use the
    # WHISPER_MODEL name (default "small") from the HF hub.
    from .common import PROJECT_ROOT
    local = PROJECT_ROOT / "models" / "faster-whisper-small"
    if (local / "model.bin").exists() and not os.environ.get("WHISPER_MODEL"):
        model_ref = str(local)
    else:
        model_ref = os.environ.get("WHISPER_MODEL", "small")
    model = WhisperModel(model_ref, device="cpu", compute_type="int8")
    segments, _info = model.transcribe(str(wav), beam_size=1, vad_filter=True)
    segs = []
    for s in segments:
        t = (s.text or "").strip()
        if not t or (s.end - s.start) < 0.2:
            continue
        segs.append({"start": round(float(s.start), 3),
                     "end": round(float(s.end), 3), "text": t})
    if not segs:
        raise RuntimeError("no speech segments found by whisper")
    save_segments(segs, workdir / "segments_en.json")
    return segs


if __name__ == "__main__":
    use_cap = "--captions" in sys.argv
    res = transcribe(sys.argv[1], Path(sys.argv[2]), use_captions=use_cap)
    print(f"{len(res)} segments")
