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


def _overlap(a, b):
    lo = max(a[0], b[0])
    hi = min(a[1], b[1])
    return max(0, hi - lo)


def _frame_bands(video, t, td):
    """Caption-LIKE bands in one frame: hot-row runs that are NARROW
    (3–24% of height) and WIDE (stroke energy spread over >=42% of
    the columns, extent >=30% of width). Scene texture and faces
    produce hot rows too, but rarely in this shape — the build-111
    span approach boxed half the scene on a real drama frame."""
    got = _frame_rows(video, t, td)
    if not got:
        return []
    rows, energy = got
    H, W = energy.shape
    prof = np.convolve(rows[:H], np.ones(max(2, int(H * 0.02)))
                       / max(2, int(H * 0.02)), mode="same")
    base = float(np.median(prof))
    if base <= 0:
        return []
    # Letterboxed uploads (a 16:9 picture floating in blurred fill —
    # the common re-upload shape) broke detection completely: the
    # fill drags the median down, so the WHOLE picture clears 1.9x
    # base as one 34%-tall run, the caption spike welds into it, and
    # the height filter kills everything (owner's drama, 2026-11-10:
    # zero pieces). Fix: find the active picture region from a broad
    # profile and re-base INSIDE it — within the picture the caption
    # spikes above the scene median exactly like the full-frame case
    # this detector was proven on. Full-frame videos are unchanged
    # (their picture region is ~the whole frame).
    broad = np.convolve(prof, np.ones(max(2, int(H * 0.10)))
                        / max(2, int(H * 0.10)), mode="same")
    pic_runs = _runs(broad > base * 1.35, max(2, int(H * 0.05)))
    y_lo, y_hi = 0, H
    if pic_runs:
        pa, pb = max(pic_runs, key=lambda r: r[1] - r[0])
        if pb - pa < 0.92 * H:
            y_lo, y_hi = pa, pb
            base = float(np.median(prof[y_lo:y_hi]))
            if base <= 0:
                return []
    hot = (prof > base * 1.9)
    hot[:y_lo] = False
    hot[y_hi:] = False
    runs = _merge(_runs(hot, max(2, int(H * 0.025))),
                  int(H * 0.03))
    # Stroke map: pixels far above the frame's typical detail level.
    # A column "carries text" when the band holds >=2 stroke pixels in
    # it — dense thin glyphs do this across the whole line width,
    # faces/texture only in scattered columns (column-MEAN energy was
    # tried first and drowned in the noise floor).
    tau = float(np.percentile(energy, 88))
    bands = []
    for a, b in runs:
        if not (0.03 * H <= b - a <= 0.24 * H):
            continue
        cols = np.where((energy[a:b] > tau).sum(axis=0) >= 2)[0]
        if not len(cols):
            continue
        if len(cols) / W < 0.42 or (cols.max() - cols.min()) / W < 0.30:
            continue
        x0 = max(0, int(cols.min()) - int(W * 0.02))
        x1 = min(W, int(cols.max()) + int(W * 0.02))
        if (x1 - x0) / W < 0.35:
            x0, x1 = int(W * 0.04), int(W * 0.96)
        bands.append({"y0": a, "y1": b, "x0": x0, "x1": x1, "H": H, "W": W})
    return bands


def detect_text_pieces(video):
    """Owner's rule (2026-11-09): blur PIECE BY PIECE — time-windowed
    pieces [{x, y, w, h, t0, t1}], each blurring only its own rows
    while its text is on screen. Caption-like bands (narrow + wide,
    see _frame_bands) are tracked over ~2 samples/sec; a band that
    sits in >=90% of all frames is STATIC scene texture (signage,
    logos) and is dropped whenever any flickering band exists;
    bands join into pieces when consecutive in time and overlapping
    in y; weak pieces (rare/short vs the strongest) are dropped."""
    video = Path(video)
    try:
        dur = probe_duration(video)
    except Exception:
        return []
    if not dur or dur < 2:
        return []
    n = int(max(8, min(48, round(dur * 2))))
    step = dur / n
    timed = []  # (t, band)
    dims = None
    with tempfile.TemporaryDirectory() as td:
        for i in range(n):
            t = (i + 0.5) * step
            for band in _frame_bands(video, t, td):
                band["fi"] = i
                dims = (band["H"], band["W"])
                timed.append((t, band))
    if not timed:
        return []
    H, W = dims
    # cluster bands by y-position; a cluster present in >=90% of the
    # sampled frames never changes -> static texture, not captions
    clusters = []
    for _t, band in timed:
        for c in clusters:
            if _overlap((c["y0"], c["y1"]), (band["y0"], band["y1"])) \
                    >= 0.5 * min(c["y1"] - c["y0"], band["y1"] - band["y0"]):
                c["frames"].add(band["fi"])
                break
        else:
            clusters.append({"y0": band["y0"], "y1": band["y1"],
                             "frames": {band["fi"]}})
    static_ys = [(c["y0"], c["y1"]) for c in clusters
                 if len(c["frames"]) >= 0.9 * n]
    has_flicker = any(len(c["frames"]) < 0.9 * n for c in clusters)
    if static_ys and has_flicker:
        timed = [(t, b) for t, b in timed
                 if not any(_overlap((b["y0"], b["y1"]), s)
                            >= 0.5 * (b["y1"] - b["y0"]) for s in static_ys)]
    timed.sort(key=lambda tb: tb[0])
    # Several bands can be alive at once (a caption line plus texture
    # that passed the shape test), so keep ALL open pieces and place
    # each band into the one whose y-range it overlaps; pieces that
    # see no band for a while are closed.
    open_pieces, pieces = [], []
    for t, band in timed:
        target = None
        for p in open_pieces:
            inter = _overlap((p["y0"], p["y1"]), (band["y0"], band["y1"]))
            if inter >= 0.5 * min(p["y1"] - p["y0"],
                                  band["y1"] - band["y0"]):
                target = p
                break
        if target is None:
            target = {"bands": [], "t0": t, "last_t": t,
                      "y0": band["y0"], "y1": band["y1"]}
            open_pieces.append(target)
        target["bands"].append(band)
        target["y0"] = min(target["y0"], band["y0"])
        target["y1"] = max(target["y1"], band["y1"])
        target["last_t"] = t
        still = []
        for p in open_pieces:
            if p is target or t - p["last_t"] <= step * 1.8:
                still.append(p)
            else:
                pieces.append(p)
        open_pieces = still
    pieces += open_pieces
    formed = []
    for p in pieces:
        bands = p["bands"]
        t1 = p["last_t"]
        span_s = t1 - p["t0"] + step
        presence = len(bands) * step / span_s
        # A piece with >=4 separate caption sightings is real even
        # when its stretch is dialogue-light (presence < 0.45) —
        # letterboxed dramas hit exactly that in quiet scenes and
        # lost their later caption coverage entirely (2026-11-10).
        if span_s < 1.0 or (presence < 0.45 and len(bands) < 4):
            continue
        formed.append({
            "y0": int(np.median([b["y0"] for b in bands])),
            "y1": int(np.median([b["y1"] for b in bands])),
            "x0": int(np.median([b["x0"] for b in bands])),
            "x1": int(np.median([b["x1"] for b in bands])),
            "t0": max(0.0, p["t0"] - step / 2), "t1": min(dur, t1 + step / 2),
            "count": len(bands)})
    if not formed:
        return []
    # Merge same-row pieces separated by short caption-less gaps
    # (scene changes, quiet stretches): the blur band is the same
    # physical strip, and leaving gaps means captions there go
    # unblurred. y/x become count-weighted means of the members.
    formed.sort(key=lambda f: f["t0"])
    merged = []
    for f in formed:
        if merged:
            a = merged[-1]
            inter = _overlap((a["y0"], a["y1"]), (f["y0"], f["y1"]))
            if inter >= 0.5 * min(a["y1"] - a["y0"], f["y1"] - f["y0"]) \
                    and f["t0"] - a["t1"] <= 15.0:
                n0, n1 = a["count"], f["count"]
                for k in ("y0", "y1", "x0", "x1"):
                    a[k] = (a[k] * n0 + f[k] * n1) // (n0 + n1)
                a["t1"] = max(a["t1"], f["t1"])
                a["count"] = n0 + n1
                continue
        merged.append(dict(f))
    formed = merged
    strongest = max(f["count"] for f in formed)
    formed = [f for f in formed if f["count"] >= max(3, 0.25 * strongest)]
    # Dominant-row fill: captions in these dramas live on ONE fixed
    # strip, but quiet/dark stretches fail the shape tests and leave
    # coverage gaps where captions then stay unblurred (the owner's
    # recurring complaint). If one row was proven across many
    # sightings, extend THAT row over uncovered windows — blurring
    # an empty strip for a few seconds is harmless; missing a
    # caption is not.
    if formed:
        dom = max(formed, key=lambda f: f["count"])
        cover = sorted(
            (f["t0"], f["t1"]) for f in formed
            if _overlap((f["y0"], f["y1"]), (dom["y0"], dom["y1"]))
            >= 0.5 * min(f["y1"] - f["y0"], dom["y1"] - dom["y0"]))
        gaps, cur = [], 0.0
        for a, b in cover:
            if a - cur > step:
                gaps.append((cur, a))
            cur = max(cur, b)
        if dur - cur > step:
            gaps.append((cur, dur))
        for a, b in gaps:
            formed.append({"y0": dom["y0"], "y1": dom["y1"],
                           "x0": dom["x0"], "x1": dom["x1"],
                           "t0": a, "t1": b, "count": dom["count"]})
        formed.sort(key=lambda f: f["t0"])
    while len(formed) > 8:
        best_i, best_d = 0, None
        for i in range(len(formed) - 1):
            d = abs(formed[i]["y0"] - formed[i + 1]["y0"])
            if best_d is None or d < best_d:
                best_i, best_d = i, d
        a, b = formed[best_i], formed[best_i + 1]
        a["y0"], a["y1"] = min(a["y0"], b["y0"]), max(a["y1"], b["y1"])
        a["x0"], a["x1"] = min(a["x0"], b["x0"]), max(a["x1"], b["x1"])
        a["t1"] = b["t1"]
        a["count"] += b["count"]
        formed.pop(best_i + 1)
    pad_y = int(H * 0.025)
    out = []
    for f in formed:
        y0 = max(0, f["y0"] - pad_y)
        y1 = min(H, f["y1"] + pad_y)
        out.append({"x": round(f["x0"] / W, 3), "y": round(y0 / H, 3),
                    "w": round((f["x1"] - f["x0"]) / W, 3),
                    "h": round((y1 - y0) / H, 3),
                    "t0": round(f["t0"], 2), "t1": round(f["t1"], 2)})
    out.sort(key=lambda p: p["t0"])
    return out


_PIECE_COLORS = [(255, 60, 60), (0, 190, 255), (255, 200, 0),
                 (0, 225, 90), (255, 0, 190), (130, 130, 255),
                 (255, 130, 0), (0, 240, 210)]


def draw_box(frame_img, box, out_path):
    """Draw the proposed box(es) on a frame image so a human can
    confirm them at a glance (Telegram confirm flow). A LIST of
    boxes (blur pieces) gets a DIFFERENT colour per box plus a
    number chip, so the customer can tell the pieces apart and
    refer to them by number (owner's request, 2026-11-09)."""
    from PIL import ImageDraw, ImageFont
    im = Image.open(frame_img).convert("RGB")
    W, H = im.size
    d = ImageDraw.Draw(im)
    multi = isinstance(box, list)
    boxes = box if multi else [box]
    try:
        font = ImageFont.load_default(size=22)
    except TypeError:
        font = ImageFont.load_default()
    for i, b in enumerate(boxes):
        color = _PIECE_COLORS[i % len(_PIECE_COLORS)] if multi \
            else (255, 60, 60)
        x0, y0 = int(b["x"] * W), int(b["y"] * H)
        x1, y1 = int((b["x"] + b["w"]) * W), int((b["y"] + b["h"]) * H)
        for t in range(4):
            d.rectangle([x0 - t, y0 - t, x1 + t, y1 + t], outline=color)
        if multi:
            label = str(i + 1)
            chip_w = 30 if len(label) == 1 else 46
            ly = max(0, y0 - 28)
            d.rectangle([x0, ly, x0 + chip_w, ly + 26], fill=color)
            d.text((x0 + 9, ly + 3), label, fill=(0, 0, 0), font=font)
    im.save(out_path, quality=88)
    return out_path


if __name__ == "__main__":
    import json
    print(json.dumps(detect_text_box(Path(sys.argv[1]))))
