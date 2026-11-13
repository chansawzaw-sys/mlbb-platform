"""Step 6: burn Myanmar subtitles into a video.

Layout-aware: the SRT is converted to an ASS script whose PlayResX/PlayResY
are the video's REAL resolution, so font size / margins are computed from
the actual frame size (correct on 16:9 and vertical 9:16 alike) and the
user's layout choices (size / width / bottom distance / top-or-bottom)
change real line wrapping via ASS MarginL/MarginR/MarginV.

The bundled Myanmar fonts (assets/fonts) are passed via fontsdir so Burmese
renders as real glyphs, not tofu boxes.

CLI: python -m pipeline.burn <input.mp4> <subs.srt> <output.mp4> [font] [subs-json]
"""
import json
import re
import sys
from pathlib import Path

from .common import (FONTS_DIR, ensure_fonts, probe_streams, run,
                     target_maxrate_k)

# Subtitle FONT CHOICES — all Myanmar UNICODE fonts bundled in assets/fonts.
# Every one was verified to cover the full Myanmar block (U+1000–109F) and to
# shape correctly in a rendered test frame (see verification/). No Zawgyi.
FONT_CHOICES = {
    "noto": "Noto Sans Myanmar",   # default, bold cinematic
    "pyidaungsu": "Pyidaungsu",
    "padauk": "Padauk",
}
DEFAULT_FONT = "noto"

# --- Subtitle layout -------------------------------------------------------
# Defaults reproduce the previous cinematic look exactly: the old code used
# libass force_style on the SRT's implicit 384x288 PlayRes with
# FontSize=26, MarginV=30 and the default MarginL/R=10 — i.e. font height
# 26/288 of the frame, bottom distance 30/288 of the frame, and a text block
# (384-2*10)/384 of the frame width.
SUBS_DEFAULTS = {"size_pct": 100, "width_pct": 95, "bottom_pct": 10,
                 "align": "bottom"}
# size_pct range is 10–120 since build 172 (owner order 2026-11-13);
# it was 60–160 before. The clamped value also caps the in-box fit
# candidate in srt_to_ass, so this one range governs every placement.
_SUBS_RANGES = {"size_pct": (10.0, 120.0), "width_pct": (50.0, 100.0),
                "bottom_pct": (2.0, 25.0)}
_BASE_FS_FRAC = 26 / 288      # font size as a fraction of frame height
_BASE_OUTLINE = 3 / 288       # outline thickness, same scaling
_BASE_SHADOW = 1 / 288


SUB_COLORS = {  # subtitle text colours, ASS &HAABBGGRR
    "white": "&H00FFFFFF", "yellow": "&H0000FFFF",
    "blue": "&H00FFA500", "green": "&H0000E600",
    "red": "&H002828FF", "black": "&H00000000",
}


def normalize_subs(subs):
    """Clamp a user subs dict to sane ranges; missing keys get defaults."""
    subs = subs or {}
    out = {}
    for key, (lo, hi) in _SUBS_RANGES.items():
        try:
            v = float(subs.get(key, SUBS_DEFAULTS[key]))
        except (TypeError, ValueError):
            v = float(SUBS_DEFAULTS[key])
        out[key] = max(lo, min(hi, v))
    out["align"] = "top" if subs.get("align") == "top" else "bottom"
    out["color"] = subs.get("color") if subs.get("color") in SUB_COLORS else "white"
    # Wizard placement choice (build 135): "box" = inside the blur
    # rect (the long-standing default, also what TG jobs get — they
    # send no placement); "bottom"/"top" = the normal layout even
    # when a blur rect exists (the blur still applies, the text just
    # does not live in it).
    out["placement"] = subs.get("placement") \
        if subs.get("placement") in ("box", "bottom", "top") else "box"
    return out


_TS_RE = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)")


def _ass_time(ts):
    m = _TS_RE.match(ts.strip())
    if not m:
        return "0:00:00.00"
    h, mm, ss, ms = (int(g) for g in m.groups())
    total_cs = ((h * 3600 + mm * 60 + ss) * 1000 + ms) // 10
    return f"{total_cs // 360000}:{(total_cs // 6000) % 60:02d}:{(total_cs // 100) % 60:02d}.{total_cs % 100:02d}"


def _parse_srt(srt_path):
    text = Path(srt_path).read_text(encoding="utf-8-sig")
    cues = []
    for block in re.split(r"\r?\n\s*\r?\n", text.strip()):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if len(lines) < 2 or "-->" not in lines[1]:
            continue
        start, end = lines[1].split("-->")[:2]
        body = "\\N".join(ln.strip() for ln in lines[2:])
        body = body.replace("{", "(").replace("}", ")")  # ASS override safety
        if body:
            cues.append((_ass_time(start), _ass_time(end), body))
    return cues


# --- Per-cue fit: shrink long cues so the text fits its box ---------------
# libass wraps at the margins, but a long cue still stacks 3-4 lines and
# covers the frame (worst on vertical video). Measure each cue with the
# REAL font (PIL) and, when it would need more than _FIT_MAX_LINES lines,
# shrink that cue's font (ASS \fs override) until it fits.
_FONT_FILES = {"Noto Sans Myanmar": "NotoSansMyanmar-Bold.ttf",
               "Pyidaungsu": "Pyidaungsu-2.3_Bold.ttf",
               "Padauk": "Padauk-Bold.ttf"}
_FIT_MAX_LINES = 2
_FIT_MIN_FRAC = 0.5


def _est_lines(text, family, fs, avail_w):
    """Estimated wrapped line count of text at font size fs within
    avail_w px, measured with the real font (PIL); None if unmeasurable."""
    try:
        from PIL import ImageFont
        fname = _FONT_FILES.get(family)
        if not fname or avail_w <= 0:
            return None
        path = FONTS_DIR / fname
        plain = text.replace("\\N", " ").strip()
        if not path.exists() or not plain:
            return None
        font = ImageFont.truetype(str(path), max(8, int(round(fs))))
        # PIL overestimates Myanmar advance widths by ~1.4x versus
        # libass/HarfBuzz shaping (combining marks) — measured on a
        # real render (626 vs ~450 px). Correct for it; the wrap
        # estimates below use the same factor.
        w = font.getlength(plain) * 0.75
        return int(w // max(1.0, avail_w * 0.94)) + 1
    except Exception:
        return None


def _fit_font_size(text, family, base_fs, avail_w):
    """Largest size <= base_fs whose text fits _FIT_MAX_LINES lines of
    avail_w px; base_fs when it already fits or measuring is impossible."""
    if _est_lines(text, family, base_fs, avail_w) is None:
        return base_fs
    fs = float(base_fs)
    floor = base_fs * _FIT_MIN_FRAC
    while fs > floor:
        lines = _est_lines(text, family, fs, avail_w)
        if lines is not None and lines <= _FIT_MAX_LINES:
            return fs
        fs *= 0.9
    return floor


def _wrap_lines(text, family, fs, avail_w):
    """Greedy word-wrap with EXPLICIT \\N breaks, measured with the
    real font (PIL). Returns (wrapped_text, n_lines). We wrap
    ourselves because libass does not reliably wrap \\pos events at
    the box margins — an unwrapped long line at a small size is a
    black smear on a phone. PIL slightly OVERestimates Myanmar
    widths (combining marks), which only makes wraps happen a
    little early — the safe direction."""
    try:
        from PIL import ImageFont
        fname = _FONT_FILES.get(family)
        if not fname or avail_w <= 0:
            return text, 1
        path = FONTS_DIR / fname
        if not path.exists():
            return text, 1
        font = ImageFont.truetype(str(path), max(8, int(round(fs))))
        out_lines = []
        for hard in str(text).split("\\N"):
            words = hard.split()
            if not words:
                continue
            cur = words[0]
            for w in words[1:]:
                trial = cur + " " + w
                if font.getlength(trial) * 0.75 <= avail_w * 0.96:
                    cur = trial
                else:
                    out_lines.append(cur)
                    cur = w
            out_lines.append(cur)
        if not out_lines:
            return text, 1
        return "\\N".join(out_lines), len(out_lines)
    except Exception:
        return text, 1


def _fit_box_strict(text, family, cand, rect_w, rect_h, min_fs):
    """Largest size <= cand at which the PIL-wrapped text fits
    STRICTLY inside the box: the full block height (lines x leading)
    stays within the box height and the text spans the full box
    width (owner's rule, 2026-11-09: use the blurred band's whole
    width, left edge to right edge, but never cross its top or
    bottom). Returns None when no size >= min_fs fits."""
    fs = float(cand)
    while fs >= min_fs:
        _t, n = _wrap_lines(text, family, fs, rect_w)
        if n <= 5 and n * fs * 1.35 <= rect_h * 0.98:
            return fs
        fs *= 0.94
    return None


def _fit_box(text, family, base_fs, rect_w, rect_h,
             min_fs=10.0, max_lines=_FIT_MAX_LINES):
    """Font size that fits text INSIDE a rect (the user's blur box).

    Preferred: the block's HEIGHT fits rect_h in at most max_lines
    lines. But readability is a hard floor: the size never drops
    below min_fs — a long line in a short box used to collapse the
    whole video's fixed size to ~10-16px, which after the Telegram
    shrink is simply invisible on a phone (owner report 2026-11-09:
    'no Myanmar text' on a job that had burned it at fs16). Below
    the floor the block may spill slightly over the box edges —
    readable beats perfectly contained."""
    if _est_lines(text, family, base_fs, rect_w) is None:
        return base_fs
    fs = float(base_fs)
    best = None  # largest fs whose height fits (any line count)
    while fs >= min_fs:
        lines = _est_lines(text, family, fs, rect_w)
        if lines is None:
            return base_fs
        if lines * fs * 1.35 <= rect_h * 0.96:
            if best is None:
                best = fs
            if lines <= max_lines:
                return fs
        fs *= 0.9
    return best if best is not None else min_fs


def srt_to_ass(srt_path, ass_path, width, height, family, subs, blur_box=None):
    """Write an ASS script with PlayRes = the real video resolution."""
    cfg = normalize_subs(subs)
    font_size = _BASE_FS_FRAC * height * cfg["size_pct"] / 100.0
    outline = max(1, round(_BASE_OUTLINE * height))
    shadow = max(1, round(_BASE_SHADOW * height))
    margin_lr = round((100.0 - cfg["width_pct"]) / 200.0 * width)
    margin_v = round(cfg["bottom_pct"] / 100.0 * height)
    alignment = 8 if cfg["align"] == "top" else 2
    lines = [
        "[Script Info]",
        "Title: Myanmar subtitles (layout)",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "ScaledBorderAndShadow: yes",
        "WrapStyle: 0",
        "",
        "[V4+ Styles]",
        ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
         "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
         "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
         "Alignment, MarginL, MarginR, MarginV, Encoding"),
        # CINEMATIC style: BOLD text (colour selectable), strong
        # outline + soft shadow, NO background box (BorderStyle=1) —
        # reads on bright/dark. Black text gets a WHITE outline so it
        # stays readable.
        (f"Style: Default,{family},{font_size:.1f},"
         f"{SUB_COLORS[cfg['color']]},&H000000FF,"
         f"{'&H00FFFFFF' if cfg['color'] == 'black' else '&H00000000'},"
         f"&H60000000,-1,0,0,0,100,100,0,0,1,{outline},{shadow},"
         f"{alignment},{margin_lr},{margin_lr},{margin_v},1"),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    # Blur-box placement: when the user drew a blur rect (fractions of
    # the frame) to hide the original subtitles, the Myanmar text goes
    # INSIDE that box — centered, with its size fitted to the box.
    # Skipped when the user picked bottom/top placement (build 135).
    box = None
    if blur_box and cfg.get("placement", "box") == "box":
        try:
            bx = min(max(float(blur_box["x"]), 0.0), 0.99)
            by = min(max(float(blur_box["y"]), 0.0), 0.99)
            bw = min(max(float(blur_box["w"]), 0.0), 1.0 - bx)
            bh = min(max(float(blur_box["h"]), 0.0), 1.0 - by)
            if bw >= 0.05 and bh >= 0.03:
                box = (bx * width, by * height, bw * width, bh * height)
        except Exception:
            box = None
    avail_w = width - 2 * margin_lr
    events = list(_parse_srt(srt_path))
    box_fs = None
    if box:
        rx, ry, rw, rh = box
        # Owner's placement rule (2026-11-09): the Myanmar text sits
        # ON the blurred band, spanning its FULL width (left edge to
        # right edge), and the block must NOT cross the band's top
        # or bottom. So the size is governed by the box itself: ONE
        # fixed size for the whole video = the largest size at which
        # EVERY cue, wrapped to the full box width, fits strictly
        # inside the box height. (This supersedes the recap-style
        # spill of builds 119-120.) Only a cue that cannot fit even
        # at the floor size wraps past the box — readable beats
        # invisible for that rare pathological line.
        cand = min(font_size, rh * 0.62)
        min_fs = max(14.0, height * 0.020)
        fits = [_fit_box_strict(b, family, cand, rw, rh, min_fs)
                for _s, _e, b in events]
        ok = [f for f in fits if f is not None]
        box_fs = min(ok) if len(ok) == len(fits) and ok else min_fs
    for start, end, body in events:
        ev_ml, ev_mr = 0, 0
        if box:
            body, _n = _wrap_lines(body, family, box_fs, rw)
            body = (f"{{\\an5\\pos({rx + rw / 2:.0f},{ry + rh / 2:.0f})"
                    f"\\fs{box_fs:.0f}}}") + body
            # Per-event margins make libass wrap INSIDE the box width
            # (a \pos event otherwise wraps at the full-frame margins).
            ev_ml, ev_mr = int(rx), int(width - (rx + rw))
        else:
            fs = _fit_font_size(body, family, font_size, avail_w)
            if fs < font_size - 0.6:
                body = f"{{\\fs{fs:.0f}}}" + body
        lines.append(f"Dialogue: 0,{start},{end},Default,,{ev_ml},{ev_mr},0,,{body}")
    Path(ass_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return ass_path


def _map_box_aspect(box, sw, sh, tw, th, style):
    """Map a blur box (fractions of the SOURCE frame) into fractions
    of the reframed canvas, following fx.filter_chain's geometry:
    blur/black fit the whole frame inside (decrease), crop fills and
    centre-crops (increase). One formula: uniform scale + centred
    offset (negative when cropping). None when the box falls (nearly)
    entirely outside the canvas."""
    if style == "crop":
        s = max(tw / sw, th / sh)
    else:
        s = min(tw / sw, th / sh)
    ox = (tw - sw * s) / 2.0
    oy = (th - sh * s) / 2.0

    def mx(f):
        return (f * sw * s + ox) / tw

    def my(f):
        return (f * sh * s + oy) / th

    x0 = min(max(mx(float(box["x"])), 0.0), 1.0)
    y0 = min(max(my(float(box["y"])), 0.0), 1.0)
    x1 = min(max(mx(float(box["x"]) + float(box["w"])), 0.0), 1.0)
    y1 = min(max(my(float(box["y"]) + float(box["h"])), 0.0), 1.0)
    if x1 - x0 < 0.03 or y1 - y0 < 0.02:
        return None
    return {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0}


def video_size(input_mp4):
    for s in probe_streams(input_mp4):
        if s.get("codec_type") == "video":
            return int(s["width"]), int(s["height"])
    raise RuntimeError(f"no video stream in {input_mp4}")


def _fonts_dir():
    """The directory libass should load the Myanmar fonts from.

    Normal case: the bundled dir (deploy decodes the .b64 copies
    there as root). If the files are STILL missing at burn time,
    decode them into a /tmp cache the service can always write —
    the burn must not depend on deploy state. Falls back to the
    bundled dir so the caller's existence check can fail loudly."""
    ensure_fonts()
    if all((FONTS_DIR / f).exists() for f in _FONT_FILES.values()):
        return FONTS_DIR
    import base64
    import tempfile
    cache = Path(tempfile.gettempdir()) / "vt-fonts"
    try:
        cache.mkdir(parents=True, exist_ok=True)
        for b in FONTS_DIR.glob("*.ttf.b64"):
            t = cache / b.with_suffix("").name
            if not t.exists() or t.stat().st_size < 10_000:
                t.write_bytes(base64.b64decode(
                    b.read_text(encoding="ascii")))
        if all((cache / f).exists() for f in _FONT_FILES.values()):
            return cache
    except Exception:
        pass
    return FONTS_DIR


def burn(input_mp4, srt_path, output_mp4, font=DEFAULT_FONT, subs=None,
         blur_box=None, fx=None, cap_k=2500):
    fonts_dir = _fonts_dir()
    family = FONT_CHOICES.get(font, FONT_CHOICES[DEFAULT_FONT])
    # Never deliver silent tofu again: if the chosen family's font
    # file is still missing, the job must FAIL LOUDLY here instead
    # of burning placeholder boxes the customer can't read.
    _ff = _FONT_FILES.get(family)
    if _ff and not (fonts_dir / _ff).exists():
        raise RuntimeError(
            f"Myanmar font မရှိပါ ({_ff}) — server admin ကို ပြောပါ")
    width, height = video_size(input_mp4)
    if fx and (fx.get("aspect") or {}).get("ratio"):
        # The fx graph reframes the canvas: lay subtitles out on the
        # FINAL size, with the blur box MAPPED into canvas coordinates.
        # (It used to be dropped, which threw the subtitles to the
        # canvas bottom — outside the blurred area entirely.)
        from . import fx as _fxm
        dims = _fxm.ASPECT_DIMS.get(fx["aspect"]["ratio"])
        if dims:
            sw, sh = width, height
            width, height = dims
            if blur_box:
                blur_box = _map_box_aspect(
                    blur_box, sw, sh, width, height,
                    fx["aspect"].get("style") or "blur")
    ass_path = Path(srt_path).with_name(Path(srt_path).stem + ".layout.ass")
    srt_to_ass(srt_path, ass_path, width, height, family, subs,
               blur_box=blur_box)
    ass = str(ass_path.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    fontsdir = str(fonts_dir).replace("\\", "/").replace(":", "\\:")
    if fx:
        from . import fx as fx_mod
        if fx_mod.has_fx(fx):
            # Blur/logo + subtitles in ONE encode: the fx filter graph
            # runs first, the ass filter is appended to its output.
            # (Was: a full fx encode followed by a full burn encode.)
            filters, cur, logo_path = fx_mod.filter_chain(fx, width, height)
            filters.append(f"{cur}ass='{ass}':fontsdir='{fontsdir}'[vout]")
            cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(input_mp4)]
            if logo_path:
                cmd += ["-i", str(logo_path)]
            mr = target_maxrate_k(input_mp4, cap_k)
            cmd += ["-filter_complex", ";".join(filters), "-map", "[vout]",
                    "-map", "0:a?", "-c:v", "libx264", "-preset", "ultrafast",
                    "-crf", "24", "-maxrate", f"{mr}k", "-bufsize", f"{2 * mr}k",
                    "-c:a", "copy", str(output_mp4)]
            run(cmd)
            return output_mp4
    vf = f"ass='{ass}':fontsdir='{fontsdir}'"
    mr = target_maxrate_k(input_mp4, cap_k)
    run(["ffmpeg", "-y", "-v", "error", "-i", str(input_mp4), "-vf", vf,
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
         "-maxrate", f"{mr}k", "-bufsize", f"{2 * mr}k",
         "-c:a", "copy", str(output_mp4)])
    return output_mp4


if __name__ == "__main__":
    _subs = json.loads(sys.argv[5]) if len(sys.argv) > 5 else None
    _box = json.loads(sys.argv[6]) if len(sys.argv) > 6 else None
    print(burn(sys.argv[1], sys.argv[2], sys.argv[3],
               sys.argv[4] if len(sys.argv) > 4 else DEFAULT_FONT, _subs,
               blur_box=_box))
