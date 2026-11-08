"""Visual effects applied at finalize time: BLUR region + LOGO overlay.

Both are configured from the UI editor screen with NORMALIZED (0-1)
coordinates, so they are resolution-independent:

  blur = {"x","y","w","h","strength"}   # rect as fraction of frame W/H
  logo = {"path","pos_x","pos_y",      # pos = 0..1 across the *travel range*
          "size_pct","opacity","saturation"}   # (W - logo_w), (H - logo_h)

Blur: crop the region -> gblur -> overlay back at the same spot (whole video).
Logo: scale to size_pct of video width, set alpha = opacity, adjust
saturation (100 = original colour, 0 = greyscale/faded), overlay at pos.

CLI: python -m pipeline.fx <in.mp4> <out.mp4> <fx.json>
"""
import json
import sys
from pathlib import Path

from .common import probe_streams, run


def video_size(path):
    for s in probe_streams(path):
        if s.get("codec_type") == "video":
            return int(s["width"]), int(s["height"])
    raise RuntimeError("no video stream found")


def has_fx(fx):
    if not fx:
        return False
    return bool(fx.get("blur")) or bool(fx.get("logo", {}).get("path"))


def filter_chain(fx, vw, vh):
    """Build the -filter_complex pieces for blur + logo.

    Returns (filters, out_label, logo_path). The input video is [0:v];
    a logo, if any, must be supplied as input 1. Shared by
    apply_effects() and by burn.py, which appends the subtitle filter
    to the same graph so blur/logo + subtitles cost ONE encode."""
    logo = fx.get("logo") or {}
    logo_path = logo.get("path")
    filters = []
    cur = "[0:v]"
    blur = fx.get("blur")
    if blur:
        bx = min(max(float(blur["x"]), 0.0), 0.99)
        by = min(max(float(blur["y"]), 0.0), 0.99)
        bw = min(max(float(blur["w"]), 0.01), 1.0 - bx)
        bh = min(max(float(blur["h"]), 0.01), 1.0 - by)
        sigma = float(blur.get("strength") or 20)
        # crop dims must be even for yuv420p
        filters.append(f"{cur}split=2[fxbase][fxcrop]")
        filters.append(
            f"[fxcrop]crop=w='trunc(iw*{bw}/2)*2':h='trunc(ih*{bh}/2)*2'"
            f":x='iw*{bx}':y='ih*{by}',gblur=sigma={sigma:.1f}[fxblurred]")
        filters.append(f"[fxbase][fxblurred]overlay=x='main_w*{bx}':y='main_h*{by}'[vfx]")
        cur = "[vfx]"
    if logo_path:
        size_pct = float(logo.get("size_pct") or 18)
        opacity = min(max(float(logo.get("opacity", 70)) / 100.0, 0.0), 1.0)
        sat = min(max(float(logo.get("saturation", 100)) / 100.0, 0.0), 2.0)
        pos_x = min(max(float(logo.get("pos_x", 1.0)), 0.0), 1.0)
        pos_y = min(max(float(logo.get("pos_y", 0.0)), 0.0), 1.0)
        logo_w = max(16, int(vw * size_pct / 100.0))
        # logo chain: scale, fade colour (hue saturation), set alpha (opacity)
        filters.append(
            f"[1:v]scale=w={logo_w}:h=-1,format=rgb24,hue=s={sat:.3f},"
            f"format=rgba,colorchannelmixer=aa={opacity:.3f}[fxlogo]")
        filters.append(
            f"{cur}[fxlogo]overlay=x='(main_w-overlay_w)*{pos_x:.4f}'"
            f":y='(main_h-overlay_h)*{pos_y:.4f}'[vfx]")
        cur = "[vfx]"
    return filters, cur, logo_path


def apply_effects(input_mp4, output_mp4, fx):
    """Apply blur (first) and logo (on top) in ONE ffmpeg pass. Returns output."""
    if not has_fx(fx):
        raise ValueError("apply_effects called with empty fx")
    vw, vh = video_size(input_mp4)
    filters, cur, logo_path = filter_chain(fx, vw, vh)
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(input_mp4)]
    if logo_path:
        cmd += ["-i", str(logo_path)]
    cmd += ["-filter_complex", ";".join(filters), "-map", cur, "-map", "0:a?",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
            "-c:a", "copy", str(output_mp4)]
    run(cmd)
    return output_mp4


if __name__ == "__main__":
    fx = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
    print(apply_effects(sys.argv[1], sys.argv[2], fx))
