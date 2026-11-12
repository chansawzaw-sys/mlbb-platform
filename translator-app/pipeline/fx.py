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

from .common import probe_streams, run, target_maxrate_k


def video_size(path):
    for s in probe_streams(path):
        if s.get("codec_type") == "video":
            return int(s["width"]), int(s["height"])
    raise RuntimeError("no video stream found")


ASPECT_DIMS = {"9:16": (720, 1280), "16:9": (1280, 720),
               "1:1": (960, 960), "4:5": (864, 1080)}
ASPECT_STYLES = ("blur", "black", "crop")


def has_fx(fx):
    if not fx:
        return False
    return (bool(fx.get("blur")) or bool(fx.get("blur_pieces"))
            or bool(fx.get("logo", {}).get("path"))
            or (fx.get("aspect") or {}).get("ratio") in ASPECT_DIMS)


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
    # Piece-by-piece blur (owner's rule): each piece blurs only its
    # own rows and only inside its time window — the text zone moves,
    # so the blur follows it instead of covering one big fixed area.
    # When pieces exist they REPLACE the single whole-video box.
    pieces = fx.get("blur_pieces") or []
    for i, p in enumerate(pieces):
        try:
            bx = min(max(float(p["x"]), 0.0), 0.99)
            by = min(max(float(p["y"]), 0.0), 0.99)
            bw = min(max(float(p["w"]), 0.01), 1.0 - bx)
            bh = min(max(float(p["h"]), 0.01), 1.0 - by)
            t0, t1 = float(p["t0"]), float(p["t1"])
        except (KeyError, TypeError, ValueError):
            continue
        sigma = float((fx.get("blur") or {}).get("strength") or 25)
        filters.append(f"{cur}split=2[pb{i}][pc{i}]")
        filters.append(
            f"[pc{i}]crop=w='trunc(iw*{bw}/2)*2':h='trunc(ih*{bh}/2)*2'"
            f":x='iw*{bx}':y='ih*{by}',gblur=sigma={sigma:.1f}[pblur{i}]")
        filters.append(
            f"[pb{i}][pblur{i}]overlay=x='main_w*{bx}':y='main_h*{by}'"
            f":enable='between(t,{t0:.2f},{t1:.2f})'[pv{i}]")
        cur = f"[pv{i}]"
    blur = None if pieces else fx.get("blur")
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
            f":y='(main_h-overlay_h)*{pos_y:.4f}'[vfxl]")
        cur = "[vfxl]"
    # Reframe to a target aspect (9:16 / 16:9 / 1:1 / 4:5) as the LAST
    # stage, so blur/logo coordinates stay in the source's own frame.
    aspect = fx.get("aspect") or {}
    dims = ASPECT_DIMS.get(aspect.get("ratio"))
    if dims:
        tw, th = dims
        style = aspect.get("style")
        if style not in ASPECT_STYLES:
            style = "blur"
        if style == "crop":
            filters.append(
                f"{cur}scale={tw}:{th}:force_original_aspect_ratio=increase,"
                f"crop={tw}:{th}[vasp]")
        elif style == "black":
            filters.append(
                f"{cur}scale={tw}:{th}:force_original_aspect_ratio=decrease,"
                f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:black[vasp]")
        else:  # blurred-fill: full frame visible over its own blurred copy
            filters.append(f"{cur}split=2[aspbg][aspfg]")
            filters.append(
                f"[aspbg]scale={tw}:{th}:force_original_aspect_ratio=increase,"
                f"crop={tw}:{th},gblur=sigma=24[aspb]")
            filters.append(
                f"[aspfg]scale={tw}:{th}:force_original_aspect_ratio=decrease"
                f"[aspf]")
            filters.append(f"[aspb][aspf]overlay=(W-w)/2:(H-h)/2[vasp]")
        cur = "[vasp]"
    return filters, cur, logo_path


def apply_effects(input_mp4, output_mp4, fx, cap_k=2500):
    """Apply blur (first) and logo (on top) in ONE ffmpeg pass. Returns output."""
    if not has_fx(fx):
        raise ValueError("apply_effects called with empty fx")
    vw, vh = video_size(input_mp4)
    filters, cur, logo_path = filter_chain(fx, vw, vh)
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(input_mp4)]
    if logo_path:
        cmd += ["-i", str(logo_path)]
    mr = target_maxrate_k(input_mp4, cap_k)
    cmd += ["-filter_complex", ";".join(filters), "-map", cur, "-map", "0:a?",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
            "-maxrate", f"{mr}k", "-bufsize", f"{2 * mr}k",
            "-c:a", "copy", str(output_mp4)]
    run(cmd)
    return output_mp4


if __name__ == "__main__":
    fx = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
    print(apply_effects(sys.argv[1], sys.argv[2], fx))
