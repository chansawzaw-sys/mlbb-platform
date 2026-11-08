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

from .common import FONTS_DIR, probe_streams, run

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
_SUBS_RANGES = {"size_pct": (60.0, 160.0), "width_pct": (50.0, 100.0),
                "bottom_pct": (2.0, 25.0)}
_BASE_FS_FRAC = 26 / 288      # font size as a fraction of frame height
_BASE_OUTLINE = 3 / 288       # outline thickness, same scaling
_BASE_SHADOW = 1 / 288


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


def _fit_font_size(text, family, base_fs, avail_w):
    """Largest size <= base_fs whose text fits _FIT_MAX_LINES lines of
    avail_w px; base_fs when it already fits or measuring is impossible."""
    try:
        from PIL import ImageFont
        fname = _FONT_FILES.get(family)
        if not fname or avail_w <= 0:
            return base_fs
        path = FONTS_DIR / fname
        plain = text.replace("\\N", " ").strip()
        if not path.exists() or not plain:
            return base_fs
        fs = float(base_fs)
        floor = base_fs * _FIT_MIN_FRAC
        while fs > floor:
            font = ImageFont.truetype(str(path), max(8, int(round(fs))))
            w = font.getlength(plain)
            lines = int(w // max(1.0, avail_w * 0.94)) + 1
            if lines <= _FIT_MAX_LINES:
                return fs
            fs *= 0.9
        return floor
    except Exception:
        return base_fs


def srt_to_ass(srt_path, ass_path, width, height, family, subs):
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
        # CINEMATIC style: BOLD, white text, strong black outline + soft
        # shadow, NO background box (BorderStyle=1) — reads on bright/dark.
        (f"Style: Default,{family},{font_size:.1f},&H00FFFFFF,&H000000FF,"
         f"&H00000000,&H60000000,-1,0,0,0,100,100,0,0,1,{outline},{shadow},"
         f"{alignment},{margin_lr},{margin_lr},{margin_v},1"),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    avail_w = width - 2 * margin_lr
    for start, end, body in _parse_srt(srt_path):
        fs = _fit_font_size(body, family, font_size, avail_w)
        if fs < font_size - 0.6:
            body = f"{{\\fs{fs:.0f}}}" + body
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{body}")
    Path(ass_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return ass_path


def video_size(input_mp4):
    for s in probe_streams(input_mp4):
        if s.get("codec_type") == "video":
            return int(s["width"]), int(s["height"])
    raise RuntimeError(f"no video stream in {input_mp4}")


def burn(input_mp4, srt_path, output_mp4, font=DEFAULT_FONT, subs=None):
    family = FONT_CHOICES.get(font, FONT_CHOICES[DEFAULT_FONT])
    width, height = video_size(input_mp4)
    ass_path = Path(srt_path).with_name(Path(srt_path).stem + ".layout.ass")
    srt_to_ass(srt_path, ass_path, width, height, family, subs)
    ass = str(ass_path.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    fontsdir = str(FONTS_DIR).replace("\\", "/").replace(":", "\\:")
    vf = f"ass='{ass}':fontsdir='{fontsdir}'"
    run(["ffmpeg", "-y", "-v", "error", "-i", str(input_mp4), "-vf", vf,
         "-c:v", "libx264", "-preset", "superfast", "-crf", "20",
         "-c:a", "copy", str(output_mp4)])
    return output_mp4


if __name__ == "__main__":
    _subs = json.loads(sys.argv[5]) if len(sys.argv) > 5 else None
    print(burn(sys.argv[1], sys.argv[2], sys.argv[3],
               sys.argv[4] if len(sys.argv) > 4 else DEFAULT_FONT, _subs))
