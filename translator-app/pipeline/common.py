"""Shared ffmpeg/ffprobe helpers."""
import json
import shutil
import subprocess
from pathlib import Path

_HAS_NICE = shutil.which("nice") is not None

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FONTS_DIR = PROJECT_ROOT / "assets" / "fonts"


def ensure_fonts():
    """Materialize the bundled Myanmar fonts. The fonts are BINARY
    and the deploy channel (git via a text-only push) cannot carry
    .ttf files, so the repo ships them as .ttf.b64 text; decode any
    that are missing next to their encoded copy. Without this the
    server had NO Myanmar font at all and libass drew every Burmese
    glyph as an empty box (owner reports 2026-11-09)."""
    import base64
    try:
        if not FONTS_DIR.exists():
            return
        for b64 in FONTS_DIR.glob("*.ttf.b64"):
            ttf = b64.with_suffix("")
            if not ttf.exists() or ttf.stat().st_size < 10_000:
                ttf.write_bytes(base64.b64decode(
                    b64.read_text(encoding="ascii")))
    except Exception:
        pass

_CA_PATCHED = False


def ensure_system_ca():
    """Point certifi at the OS CA bundle when one exists.

    Some networks (corporate / sandbox MITM proxies) use a CA that is in the
    OS store (/etc/ssl/certs/ca-certificates.crt) but NOT in certifi's bundle.
    yt-dlp, aiohttp (edge-tts) and requests all consult certifi, so without
    this they fail with CERTIFICATE_VERIFY_FAILED while curl works fine.
    Safe no-op where the OS bundle does not exist.
    """
    global _CA_PATCHED
    if _CA_PATCHED:
        return
    _CA_PATCHED = True
    sys_bundle = Path("/etc/ssl/certs/ca-certificates.crt")
    if sys_bundle.exists():
        try:
            import certifi
            certifi.where = lambda: str(sys_bundle)  # noqa: E731
        except Exception:
            pass


def maxrate_cap_k(quality):
    """Encode maxrate ceiling (kbps) for a job's download quality
    (build 167). "hd" jobs fetched the original rendition on the
    owner's explicit order — the build-102 ceiling of 2500k would
    strangle it straight back to blurry, so hd gets 6000k. The
    source anchor in target_maxrate_k still keeps a low-bitrate
    source from ballooning; the cap only stops being the limit."""
    return 6000 if quality == "hd" else 2500


def target_maxrate_k(input_mp4, cap_k=2500):
    """Video maxrate (kbps) that keeps a re-encode in the SOURCE's size
    class. CRF alone is quality-based: re-encoding a low-bitrate phone
    or TikTok source at crf 23/24 ballooned one 12 MB job to 110 MB.
    Anchor on the original source.mp4 sitting next to the input when
    there is one (dub jobs burn the retimed intermediate, whose own
    bitrate is already one re-encode away from the source)."""
    try:
        src = Path(input_mp4)
        anchor = src.with_name("source.mp4")
        if anchor.exists():
            src = anchor
        dur = probe_duration(src)
        if dur <= 0:
            return 1800
        kbps = src.stat().st_size * 8 / dur / 1000.0
        return int(max(450, min(cap_k, kbps * 1.1)))
    except Exception:
        return 1800


def run(cmd, **kw):
    """Run a command, raise RuntimeError with stderr tail on failure.

    ffmpeg runs under `nice` so a long encode can never starve the
    rest of this small shared server (the MLBB app lives on it too);
    the encode still gets every idle cycle, it just yields politely."""
    if cmd and cmd[0] == "ffmpeg" and _HAS_NICE:
        cmd = ["nice", "-n", "8"] + [str(c) for c in cmd]
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "")[-2000:]
        raise RuntimeError(f"command failed ({' '.join(str(c) for c in cmd[:4])}...):\n{tail}")
    return p


def probe_duration(path):
    p = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(path)])
    return float(json.loads(p.stdout)["format"]["duration"])


def probe_streams(path):
    p = run(["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,width,height,r_frame_rate,duration,channels,channel_layout",
             "-of", "json", str(path)])
    return json.loads(p.stdout)["streams"]


def fmt_ts(seconds):
    """SRT timestamp."""
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms == 1000:
        s += 1
        ms = 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(segments, out_path, text_key="text_my", start_key="start", end_key="end"):
    lines = []
    for i, seg in enumerate(segments, 1):
        text = (seg.get(text_key) or "").strip()
        if not text:
            continue
        lines.append(str(i))
        lines.append(f"{fmt_ts(seg[start_key])} --> {fmt_ts(seg[end_key])}")
        lines.append(text)
        lines.append("")
    Path(out_path).write_text("\n".join(lines), encoding="utf-8")
    return out_path


def load_segments(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_segments(segments, path):
    Path(path).write_text(json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8")
