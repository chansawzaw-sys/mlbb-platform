"""Step 1: download a video from a link with yt-dlp.

CLI: python -m pipeline.download <url> <workdir>
Writes <workdir>/source.mp4 and <workdir>/source_info.json
Also tries to fetch existing captions (fast path for transcribe.py --captions).
"""
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

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
    # One retry: a transient resolver failure silently drops the job
    # to the yt-dlp ladder, whose TikTok rendition can be 4x larger.
    r = None
    for attempt in range(2):
        try:
            r = requests.get("https://www.tikwm.com/api/",
                             params={"url": url}, headers=headers,
                             timeout=30)
            r.raise_for_status()
            break
        except Exception:
            if attempt == 1:
                raise
            import time as _t
            _t.sleep(2)
    payload = r.json()
    data = payload.get("data") or {}
    play = data.get("play") or data.get("hdplay") or data.get("wmplay")
    if not play:
        raise RuntimeError(f"tikwm resolver returned no video URL: {payload!r:.200}")
    out = workdir / "source.mp4"
    # Stream to a .part file and rename only when complete: an
    # interrupted download must never leave a truncated source.mp4
    # behind for the shared preview cache to reuse forever.
    tmp = workdir / "source.mp4.part"
    with requests.get(play, headers=headers, timeout=180, stream=True) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(1 << 20):
                if chunk:
                    f.write(chunk)
    if tmp.stat().st_size < 10_000:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("tikwm download came back empty/truncated")
    tmp.replace(out)
    author = data.get("author") or {}
    return {
        "title": data.get("title"),
        "duration": data.get("duration"),
        "webpage_url": url,
        "uploader": author.get("nickname") or author.get("unique_id"),
    }


_XHS_HOSTS = ("xhslink.com", "xiaohongshu.com", "rednote.com")
_XHS_ID_RE = re.compile(r"/(?:explore|discovery/item)/([0-9a-f]{20,32})")


def _xhs_candidates(url: str):
    """Harvest RedNote/Xiaohongshu note URLs from the share link's
    redirect chain. The chain's FINAL hop is a login wall (that is
    what killed the plain download — yt-dlp followed it there and
    found an "unsupported" login URL), but the item URL flashes past
    mid-chain, query string included — and that query can carry the
    xsec_token the site now demands for web viewing. Follow hops by
    hand with an impersonated client and collect every item URL,
    rewritten onto the canonical xiaohongshu.com host."""
    seen = [url]
    try:
        from curl_cffi import requests as creq
        cur = url
        for _ in range(6):
            r = creq.get(cur, impersonate="chrome",
                         allow_redirects=False, timeout=20)
            loc = r.headers.get("Location") or r.headers.get("location")
            if r.status_code not in (301, 302, 303, 307, 308) or not loc:
                break
            cur = urljoin(cur, loc)
            seen.append(cur)
            if "login" in cur:
                break
    except Exception:
        pass
    out = []
    for u in seen:
        m = _XHS_ID_RE.search(u)
        if not m:
            continue
        q = urlparse(u).query
        for host in ("www.xiaohongshu.com", "www.rednote.com"):
            cand = f"https://{host}/explore/{m.group(1)}"
            if q:
                cand += "?" + q
            if cand not in out:
                out.append(cand)
    return out


def _xhs_page_video_urls(html: str):
    """Direct video URLs embedded in a note page: og:video first,
    then any .mp4 inside the page state (URLs there are escaped)."""
    urls = []
    m = re.search(r'og:video(?:"|\')\s+content=(?:"|\')([^"\']+)', html)
    if m:
        urls.append(m.group(1))
    txt = (html.replace("\\/", "/").replace("\\u002F", "/")
           .replace("\\u002f", "/").replace("&amp;", "&"))
    for m in re.finditer(r"https?://[^\s\"'<>\\]+?\.mp4(?:\?[^\s\"'<>\\]*)?",
                          txt):
        if m.group(0) not in urls:
            urls.append(m.group(0))
    urls.sort(key=lambda u: (0 if "sns-video" in u else 1, len(u)))
    return urls


def _xhs_fallback(url: str, workdir: Path) -> dict:
    """RedNote/Xiaohongshu download. The plain ladder dies on the
    login wall the share link redirects into; instead: harvest item
    URLs from the redirect chain, try yt-dlp on each, then parse the
    note page's embedded state for a direct mp4 and stream it."""
    import requests

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/126.0 Safari/537.36",
               "Referer": "https://www.xiaohongshu.com/"}
    cands = _xhs_candidates(url)
    if not cands:
        raise RuntimeError("XHS: no note id found in the share link chain")
    dl_opts = {
        "format": ("bv*[height<=720][tbr<=1800]+ba/b[height<=720][tbr<=1800]/"
                   "bv*[height<=720]+ba/b[height<=720]/b"),
        "merge_output_format": "mp4",
        "outtmpl": str(workdir / "source.%(ext)s"),
        "noplaylist": True, "quiet": True, "no_warnings": True,
        "socket_timeout": 15, "retries": 3,
    }
    last_err = None
    for cand in cands:
        try:
            attempt = dict(dl_opts)
            try:
                from yt_dlp.networking.impersonation import ImpersonateTarget
                attempt["impersonate"] = ImpersonateTarget("chrome")
            except Exception:
                pass
            with yt_dlp.YoutubeDL(attempt) as ydl:
                info = ydl.extract_info(cand, download=True)
            if info:
                return info
        except Exception as e:
            last_err = e
    # page-state parse: the note page embeds the video URL directly
    try:
        from curl_cffi import requests as creq
        resp = creq.get(cands[0], impersonate="chrome", timeout=30,
                        headers={"Referer": "https://www.xiaohongshu.com/"})
        video_urls = _xhs_page_video_urls(resp.text or "")
    except Exception as e:
        video_urls = []
        last_err = e
    out = workdir / "source.mp4"
    tmp = workdir / "source.mp4.part"
    for vu in video_urls:
        try:
            with requests.get(vu, headers=headers, timeout=180,
                              stream=True) as resp:
                resp.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in resp.iter_content(1 << 20):
                        if chunk:
                            f.write(chunk)
            if tmp.exists() and tmp.stat().st_size > 100_000:
                tmp.replace(out)
                return {"title": None, "duration": None,
                        "webpage_url": cands[0], "uploader": None}
        except Exception as e:
            last_err = e
        tmp.unlink(missing_ok=True)
    raise RuntimeError(f"XHS resolver found no downloadable video: {last_err}")


def download(url: str, workdir: Path) -> Path:
    ensure_system_ca()
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    out = workdir / "source.mp4"
    opts = {
        # Light renditions first: at the same resolution, platforms
        # offer several bitrates (TikTok's "download" rendition of a
        # 63 s clip is 39 MB where the standard one is 9 MB). A bitrate
        # cap picks the light one; the uncapped selectors remain as
        # fallbacks so an unusual format list can never fail the job.
        "format": ("bv*[height<=720][tbr<=1800]+ba/b[height<=720][tbr<=1800]/"
                   "bv*[height<=720]+ba/b[height<=720]/b"),
        "merge_output_format": "mp4",
        "outtmpl": str(workdir / "source.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        # Bound every attempt: without these, a hostile/slow site can
        # stretch each ladder attempt to minutes of silent waiting.
        "socket_timeout": 15,
        "retries": 3,
        # Segmented downloads (YouTube DASH etc.) fetch fragments one by
        # one by default; 4 at a time is a large, safe win on this link.
        "concurrent_fragment_downloads": 8,
    }
    info = None
    first_err = None
    if any(h in urlparse(url).netloc for h in _XHS_HOSTS):
        # RedNote share links redirect into a login wall; the XHS
        # resolver harvests the note URL from the chain first. The
        # generic ladder stays as the backup.
        try:
            info = _xhs_fallback(url, workdir)
        except Exception as e:
            first_err = e
    if info is None and "tiktok" in urlparse(url).netloc:
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
        if ("tiktok" in urlparse(url).netloc
                or any(h in urlparse(url).netloc for h in _XHS_HOSTS)):
            raise RuntimeError("skip caption fetch for TikTok/XHS")
        sub_opts = {
            "skip_download": True, "writesubtitles": True, "writeautomaticsub": True,
            "subtitleslangs": ["en.*"], "subtitlesformat": "vtt",
            "outtmpl": str(workdir / "source.%(ext)s"),
            "noplaylist": True, "quiet": True, "no_warnings": True,
            "socket_timeout": 15, "retries": 2,
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
