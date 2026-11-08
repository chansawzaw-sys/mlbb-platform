"""Step 1: download a video from a link with yt-dlp.

CLI: python -m pipeline.download <url> <workdir>
Writes <workdir>/source.mp4 and <workdir>/source_info.json
Also tries to fetch existing captions (fast path for transcribe.py --captions).
"""
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp

from .common import ensure_system_ca


def _tiktok_fallback(url: str, workdir: Path) -> dict:
    """Last-resort TikTok download via the public tikwm resolver API.

    TikTok increasingly serves datacenter IPs a stripped/blocker page, so
    even impersonated yt-dlp cannot extract the video. The resolver sees
    the video from its own (residential-grade) vantage point and returns
    a direct, watermark-free mp4 URL. No login/cookies involved.
    """
    import requests

    headers = {"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36"}
    r = requests.get("https://www.tikwm.com/api/", params={"url": url},
                     headers=headers, timeout=30)
    r.raise_for_status()
    payload = r.json()
    data = payload.get("data") or {}
    play = data.get("play") or data.get("hdplay") or data.get("wmplay")
    if not play:
        raise RuntimeError(f"tikwm resolver returned no video URL: {payload!r:.200}")
    out = workdir / "source.mp4"
    with requests.get(play, headers=headers, timeout=180, stream=True) as resp:
        resp.raise_for_status()
        with open(out, "wb") as f:
            for chunk in resp.iter_content(1 << 20):
                if chunk:
                    f.write(chunk)
    author = data.get("author") or {}
    return {
        "title": data.get("title"),
        "duration": data.get("duration"),
        "webpage_url": url,
        "uploader": author.get("nickname") or author.get("unique_id"),
    }


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
    info = None
    first_err = None
    if "tiktok" in urlparse(url).netloc:
        # TikTok blocks this server's IP at the page level, so every
        # yt-dlp attempt below just burns minutes before failing. The
        # public resolver sees the video from a different vantage point
        # and is fast — try it FIRST; the ladder stays as the backup.
        try:
            info = _tiktok_fallback(url, workdir)
        except Exception as e:
            first_err = e
    if info is None:
        # Attempt ladder: plain first, then browser impersonation. Social
        # sites fingerprint plain datacenter clients and answer with an
        # error page ("Unexpected response from webpage request");
        # impersonating a real browser's TLS/headers usually gets through.
        for target in (None, "chrome", "safari"):
            attempt_opts = dict(opts)
            if target is not None:
                try:
                    from yt_dlp.networking.impersonation import ImpersonateTarget
                    attempt_opts["impersonate"] = ImpersonateTarget(target)
                except Exception:
                    continue
            try:
                with yt_dlp.YoutubeDL(attempt_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                break
            except Exception as e:
                if first_err is None:
                    first_err = e
    if info is None:
        raise first_err
    # Caption fast-path is BEST-EFFORT only: a subtitle 429/failure must
    # never kill the video download (observed on rate-limited IPs).
    # Skipped for TikTok: from this server's IP the caption request just
    # fails slowly — whisper transcribes the audio anyway.
    try:
        if "tiktok" in urlparse(url).netloc:
            raise RuntimeError("skip caption fetch for TikTok")
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
