"""Detect the burned-in caption band of a video (auto blur box).

No OCR and no new dependencies: burned-in captions are a PERSISTENT
strip of high-frequency detail (letter edges) sitting in the same
rows across most of the video, while scene content changes. Sample
frames evenly, measure per-row high-frequency energy with PIL +
numpy, take a high percentile across frames (captions are absent
during speech gaps, so the median would miss them), and report the
strongest persistent band as box fractions {x, y, w, h} — the same
shape the web editor's hand-drawn blur box uses. Returns None when
nothing looks like a stable caption band.

CLI: python -m pipeline.detect_text <video> -> JSON box or null
"""
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

from .common import probe_duration

_SAMPLES = 7
_WIDTH = 320  # analysis width; plenty for band finding, fast


def _frame_rows(video, at, tmpdir):
    """One frame's per-row high-frequency energy profile (or None)."""
    out = Path(tmpdir) / f"f{at:.2f}.jpg"
    r = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", f"{at:.2f}", "-i", str(video),
         "-frames:v", "1", "-vf", f"scale={_WIDTH}:-2", str(out)],
        capture_output=True, text=True)
    if r.returncode != 0 or not out.exists():
        return None
    g = np.asarray(Image.open(out).convert("L"), dtype=np.float32)
    if g.ndim != 2 or g.shape[0] < 40:
        return None
    # high-frequency energy: difference from a vertically smoothed self
    k = 5
    pad = np.pad(g, ((k // 2, k // 2), (0, 0)), mode="edge")
    smooth = sum(pad[i:i + g.shape[0]] for i in range(k)) / k
    energy = np.abs(g - smooth)
    return energy.mean(axis=1), energy


def detect_text_box(video):
    video = Path(video)
    try:
        dur = probe_duration(video)
    except Exception:
        return None
    if not dur or dur < 2:
        return None
    profiles, shape, mid_energy = [], None, None
    with tempfile.TemporaryDirectory() as td:
        for f in (0.15, 0.28, 0.41, 0.5, 0.59, 0.72, 0.85):
            got = _frame_rows(video, dur * f, td)
            if got:
                rows, energy = got
                profiles.append(rows)
                shape = energy.shape
                if abs(f - 0.5) < 0.1:
                    mid_energy = energy
    if len(profiles) < 3 or shape is None or mid_energy is None:
        return None
    H, W = shape
    stack = np.stack([p[:H] for p in profiles])
    band_profile = np.percentile(stack, 70, axis=0)
    # smooth over ~2% of height so single noisy rows can't split a band
    win = max(2, int(H * 0.02))
    prof = np.convolve(band_profile, np.ones(win) / win, mode="same")
    base = float(np.median(prof))
    if base <= 0:
        return None
    hot = prof > base * 1.9
    # contiguous hot runs, height between 4% and 32% of the frame
    runs, start = [], None
    for i, v in enumerate(list(hot) + [False]):
        if v and start is None:
            start = i
        elif not v and start is not None:
            runs.append((start, i))
            start = None
    runs = [r for r in runs if 0.04 <= (r[1] - r[0]) / H <= 0.32]
    if not runs:
        return None
    # strongest run by mean energy
    best = max(runs, key=lambda r: float(prof[r[0]:r[1]].mean()))
    y0, y1 = best
    # trim columns inside the band using the middle frame's energy map
    col = mid_energy[y0:y1].mean(axis=0)
    cbase = float(np.median(col)) or 1.0
    cols = np.where(col > cbase * 1.5)[0]
    if len(cols):
        x0 = max(0, int(cols.min()) - int(W * 0.02))
        x1 = min(W, int(cols.max()) + int(W * 0.02))
    else:
        x0, x1 = 0, W
    if (x1 - x0) / W < 0.35:  # captions span wider than this; widen
        x0, x1 = int(W * 0.04), int(W * 0.96)
    pad_y = int(H * 0.015)
    y0 = max(0, y0 - pad_y)
    y1 = min(H, y1 + pad_y)
    return {"x": round(x0 / W, 3), "y": round(y0 / H, 3),
            "w": round((x1 - x0) / W, 3), "h": round((y1 - y0) / H, 3)}


def draw_box(frame_img, box, out_path):
    """Draw the proposed box on a frame image so a human can confirm
    it at a glance (used by the Telegram confirm flow)."""
    from PIL import ImageDraw
    im = Image.open(frame_img).convert("RGB")
    W, H = im.size
    d = ImageDraw.Draw(im)
    x0, y0 = int(box["x"] * W), int(box["y"] * H)
    x1, y1 = int((box["x"] + box["w"]) * W), int((box["y"] + box["h"]) * H)
    for t in range(4):
        d.rectangle([x0 - t, y0 - t, x1 + t, y1 + t], outline=(255, 60, 60))
    im.save(out_path, quality=88)
    return out_path


if __name__ == "__main__":
    import json
    print(json.dumps(detect_text_box(Path(sys.argv[1]))))
