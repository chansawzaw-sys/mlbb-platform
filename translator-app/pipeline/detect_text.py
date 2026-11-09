"""Detect the burned-in caption band of a video (auto blur box).

No OCR and no new dependencies: burned-in captions are strips of
high-frequency detail (letter edges). Sample frames evenly and
measure per-row high-frequency energy with PIL + numpy.

Owner's rule (2026-11-09): blur where the text APPEARS AND DISAPPEARS
(dialogue captions come and go with speech), not the thing that sits
on screen the whole video (a fixed logo/title is hot in ~every frame
and used to win). So the primary signal is DYNAMIC rows — hot in at
least 2 samples but not in ~90%+ of them — clustered into a span,
then grown into any adjacent steady caption rows. Only when no
dynamic text exists do we fall back to the original persistent-band
method (a high percentile across frames, strongest band). The box is
{x, y, w, h} fractions — the same shape the web editor's hand-drawn
blur box uses. Returns None when nothing looks like text.

CLI: python -m pipeline.detect_text <video> -> JSON box or null
"""
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

from .common import probe_duration

_SAMPLES = 11
_WIDTH = 320  # analysis width; plenty for band finding, fast


def _runs(mask, min_len):
    """Contiguous True runs of at least min_len, as (start, end)."""
    runs, start = [], None
    for i, v in enumerate(list(mask) + [False]):
        if v and start is None:
            start = i
        elif not v and start is not None:
            runs.append((start, i))
            start = None
    return [r for r in runs if r[1] - r[0] >= min_len]


def _merge(runs, gap):
    """Merge runs separated by at most gap rows."""
    out = []
    for r in runs:
        if out and r[0] - out[-1][1] <= gap:
            out[-1] = (out[-1][0], r[1])
        else:
            out.append(tuple(r))
    return out


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
    frames = []  # (per-row profile, energy map) per sampled frame
    with tempfile.TemporaryDirectory() as td:
        for i in range(_SAMPLES):
            f = 0.06 + 0.88 * i / max(1, _SAMPLES - 1)
            got = _frame_rows(video, dur * f, td)
            if got:
                frames.append(got)
    if len(frames) < 3:
        return None
    H, W = frames[0][1].shape
    n = len(frames)
    win = max(2, int(H * 0.02))
    counts = np.zeros(H, dtype=int)
    for rows, _e in frames:
        prof = np.convolve(rows[:H], np.ones(win) / win, mode="same")
        base = float(np.median(prof))
        if base > 0:
            counts += (prof > base * 1.9)
    # -- dynamic text first: rows hot in >=2 samples but NOT ~always --
    static_thr = int(np.ceil(0.9 * n))
    dyn = (counts >= 2) & (counts < static_thr)
    yspan = None
    if dyn.any():
        runs = _merge(_runs(dyn, max(2, int(H * 0.03))), int(H * 0.04))
        if runs:
            # Owner's rule: ONE fixed box over the WHOLE extent where
            # text appears/disappears — not piece by piece. Only
            # clusters whose hot-mass is a small fraction of the
            # strongest cluster's count as noise and are dropped
            # before the span is taken.
            mass = [float(counts[a:b].sum()) for a, b in runs]
            keep = [r for r, m in zip(runs, mass)
                    if m >= 0.15 * max(mass)]
            if keep:
                yspan = (keep[0][0], keep[-1][1])
            # grow into adjacent steady caption rows (a caption whose
            # first line is on during most speech joins its flashing
            # second line) — stops at rows that are rarely hot, so a
            # distant always-on logo is never absorbed
            y0, y1 = yspan
            steady = max(2, n // 2)
            while y0 > 0 and counts[y0 - 1] >= steady:
                y0 -= 1
            while y1 < H and counts[y1] >= steady:
                y1 += 1
            yspan = (y0, y1)
    if yspan is None:
        # -- fallback: the original persistent-band method --
        stack = np.stack([p[:H] for p, _e in frames])
        band_profile = np.percentile(stack, 70, axis=0)
        prof = np.convolve(band_profile, np.ones(win) / win, mode="same")
        base = float(np.median(prof))
        if base <= 0:
            return None
        runs = [r for r in _runs(prof > base * 1.9, 1)
                if 0.04 <= (r[1] - r[0]) / H <= 0.32]
        if not runs:
            return None
        yspan = max(runs, key=lambda r: float(prof[r[0]:r[1]].mean()))
    y0, y1 = yspan
    # trim columns inside the band from the summed energy of all frames
    esum = sum(e[y0:y1] for _r, e in frames)
    col = esum.mean(axis=0)
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
