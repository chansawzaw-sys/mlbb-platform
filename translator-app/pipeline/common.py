"""Shared ffmpeg/ffprobe helpers."""
import json
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FONTS_DIR = PROJECT_ROOT / "assets" / "fonts"

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


def run(cmd, **kw):
    """Run a command, raise RuntimeError with stderr tail on failure."""
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
             "stream=codec_type,codec_name,width,height,r_frame_rate,duration",
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
