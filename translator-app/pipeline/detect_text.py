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
            if runs[-1][1] - runs[0][0] <= 0.6 * H:
                yspan = (runs[0][0], runs[-1][1])
            else:
                yspan = max(runs, key=lambda r: r[1] - r[0])
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


def _piece_x(energies, y0, y1, W):
    """Column extent of text inside rows [y0, y1) from energy maps."""
    esum = sum(e[y0:y1] for e in energies)
    col = esum.mean(axis=0)
    cbase = float(np.median(col)) or 1.0
    cols = np.where(col > cbase * 1.5)[0]
    if len(cols):
        x0 = max(0, int(cols.min()) - int(W * 0.02))
        x1 = min(W, int(cols.max()) + int(W * 0.02))
    else:
        x0, x1 = 0, W
    if (x1 - x0) / W < 0.35:
        x0, x1 = int(W * 0.04), int(W * 0.96)
    return x0, x1


def _overlap(a, b):
    lo = max(a[0], b[0])
    hi = min(a[1], b[1])
    return max(0, hi - lo)


def detect_text_pieces(video):
    """Owner's rule (2026-11-09, correcting the one-big-box idea):
    blur PIECE BY PIECE — the text zone MOVES over the video (bottom
    captions now, a centre title later), so return time-windowed
    pieces [{x, y, w, h, t0, t1}]: each piece blurs only its own
    rows, only while its text is on screen. Frames are sampled about
    once a second; a frame's text rows are its hot runs; consecutive
    frames whose spans overlap join one piece. [] when no text."""
    video = Path(video)
    try:
        dur = probe_duration(video)
    except Exception:
        return []
    if not dur or dur < 2:
        return []
    n = int(max(6, min(40, round(dur))))
    step = dur / n
    frames = []  # (t, span_or_None, energy)
    with tempfile.TemporaryDirectory() as td:
        for i in range(n):
            t = (i + 0.5) * step
            got = _frame_rows(video, t, td)
            if not got:
                frames.append((t, None, None))
                continue
            rows, energy = got
            H = energy.shape[0]
            win = max(2, int(H * 0.02))
            prof = np.convolve(rows[:H], np.ones(win) / win, mode="same")
            base = float(np.median(prof))
            span = None
            if base > 0:
                runs = _merge(_runs(prof > base * 1.9,
                                    max(2, int(H * 0.03))), int(H * 0.03))
                runs = [r for r in runs if (r[1] - r[0]) <= 0.4 * H]
                if runs:
                    span = (runs[0][0], runs[-1][1])
            frames.append((t, span, energy))
    if not frames or frames[0][2] is None:
        return []
    H, W = frames[0][2].shape
    pieces = []
    for t, span, energy in frames:
        if span is None:
            continue
        if pieces and t - pieces[-1]["t1"] <= step * 1.6:
            p = pieces[-1]
            ps = (p["y0"], p["y1"])
            inter = _overlap(ps, span)
            if inter >= 0.4 * min(ps[1] - ps[0], span[1] - span[0]):
                p["y0"] = min(p["y0"], span[0])
                p["y1"] = max(p["y1"], span[1])
                p["t1"] = t + step / 2
                p["energies"].append(energy)
                continue
        pieces.append({"y0": span[0], "y1": span[1],
                       "t0": max(0.0, t - step / 2), "t1": t + step / 2,
                       "energies": [energy]})
    # drop slivers, then cap the count by merging the closest pair
    pieces = [p for p in pieces if p["t1"] - p["t0"] >= 0.8] or pieces
    while len(pieces) > 8:
        best_i, best_d = 0, None
        for i in range(len(pieces) - 1):
            d = abs(pieces[i]["y0"] - pieces[i + 1]["y0"])
            if best_d is None or d < best_d:
                best_i, best_d = i, d
        a, b = pieces[best_i], pieces[best_i + 1]
        a["y0"], a["y1"] = min(a["y0"], b["y0"]), max(a["y1"], b["y1"])
        a["t1"] = b["t1"]
        a["energies"] += b["energies"]
        pieces.pop(best_i + 1)
    pad_y = int(H * 0.015)
    out = []
    for p in pieces:
        y0 = max(0, p["y0"] - pad_y)
        y1 = min(H, p["y1"] + pad_y)
        x0, x1 = _piece_x(p["energies"], y0, y1, W)
        out.append({"x": round(x0 / W, 3), "y": round(y0 / H, 3),
                    "w": round((x1 - x0) / W, 3), "h": round((y1 - y0) / H, 3),
                    "t0": round(p["t0"], 2), "t1": round(min(p["t1"], dur), 2)})
    return out


def draw_box(frame_img, box, out_path):
    """Draw the proposed box(es) on a frame image so a human can
    confirm them at a glance (used by the Telegram confirm flow).
    `box` may be one box dict or a list of them (blur pieces)."""
    from PIL import ImageDraw
    im = Image.open(frame_img).convert("RGB")
    W, H = im.size
    d = ImageDraw.Draw(im)
    boxes = box if isinstance(box, list) else [box]
    for b in boxes:
        x0, y0 = int(b["x"] * W), int(b["y"] * H)
        x1, y1 = int((b["x"] + b["w"]) * W), int((b["y"] + b["h"]) * H)
        for t in range(4):
            d.rectangle([x0 - t, y0 - t, x1 + t, y1 + t],
                        outline=(255, 60, 60))
    im.save(out_path, quality=88)
    return out_path


if __name__ == "__main__":
    import json
    print(json.dumps(detect_text_box(Path(sys.argv[1]))))
