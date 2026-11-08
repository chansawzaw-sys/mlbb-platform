"""Step 1: download a video from a link with yt-dlp.

CLI: python -m pipeline.download <url> <workdir>
Writes <workdir>/source.mp4 and <workdir>/source_info.json
Also tries to fetch existing captions (fast path for transcribe.py --captions).
"""
import json
import sys
from pathlib import Path

import yt_dlp

from .common import ensure_system_ca


def download(url: str, workdir: Path) -> Path:
    ensure_system_ca()
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    out = workdir / "source.mp4"
    opts = {
        "format": "bv*[height<=720]+ba/b[height<=720]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(workdir / "source.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as first_err:
        # Retry once with browser impersonation: TikTok and other social
        # sites fingerprint plain datacenter clients and answer with an
        # error page ("Unexpected response from webpage request").
        try:
            from yt_dlp.networking.impersonation import ImpersonateTarget
            imp_opts = dict(opts, impersonate=ImpersonateTarget("chrome"))
            with yt_dlp.YoutubeDL(imp_opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception:
            raise first_err
    # Caption fast-path is BEST-EFFORT only: a subtitle 429/failure must
    # never kill the video download (observed on rate-limited IPs).
    try:
        sub_opts = {
            "skip_download": True, "writesubtitles": True, "writeautomaticsub": True,
            "subtitleslangs": ["en.*"], "subtitlesformat": "vtt",
            "outtmpl": str(workdir / "source.%(ext)s"),
            "noplaylist": True, "quiet": True, "no_warnings": True,
        }
        with yt_dlp.YoutubeDL(sub_opts) as ydl:
            ydl.extract_info(url, download=True)
    except Exception:
        pass
    # merged file may land as source.mp4; if only a single other video file exists, use it
    if not out.exists():
        cands = [p for p in workdir.glob("source.*")
                 if p.suffix.lower() in (".mp4", ".mkv", ".webm") and p.is_file()]
        if not cands:
            raise RuntimeError("yt-dlp finished but no video file was produced")
        cands[0].rename(out)
    meta = {
        "title": info.get("title"),
        "duration": info.get("duration"),
        "webpage_url": info.get("webpage_url") or url,
        "uploader": info.get("uploader"),
    }
    (workdir / "source_info.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


if __name__ == "__main__":
    path = download(sys.argv[1], Path(sys.argv[2]))
    print(path)
