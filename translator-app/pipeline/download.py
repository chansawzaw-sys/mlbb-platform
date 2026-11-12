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

_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_SHORT_HOSTS = ("vt.tiktok.com", "vm.tiktok.com", "xhslink.com")

# Download quality (build 167, owner order 2026-11-13): the customer
# picks the source rendition — "hd" = the original/best available
# (the owner's explicit wish), "720"/"480" cap the height for a
# lighter file. Anything unrecognised means "hd".
QUALITIES = ("hd", "720", "480")


def norm_quality(q):
    return q if q in QUALITIES else "hd"


# yt-dlp format selectors per quality. "720" keeps the build-80
# light-rendition selectors exactly (bitrate-capped first so a
# same-height heavy rendition is not picked by accident); "hd"
# takes the best available, period; "480" mirrors 720's shape.
_FORMAT_SELECTORS = {
    "hd": "bv*+ba/b",
    "720": ("bv*[height<=720][tbr<=1800]+ba/b[height<=720][tbr<=1800]/"
            "bv*[height<=720]+ba/b[height<=720]/b"),
    "480": ("bv*[height<=480][tbr<=1200]+ba/b[height<=480][tbr<=1200]/"
            "bv*[height<=480]+ba/b[height<=480]/b"),
}


def clean_url(raw: str) -> str:
    """Best URL out of whatever was pasted (build 166). Share
    blurbs carry text around the link; TikTok tracking parameters
    (?_r=, &_t=...) make resolver APIs choke, so TikTok URLs are
    reduced to their bare path. Other hosts keep their query —
    YouTube needs ?v=, XHS needs its token."""
    text = (raw or "").strip()
    m = _URL_RE.search(text)
    url = (m.group(0) if m else text).strip().rstrip(").,]'\"")
    try:
        p = urlparse(url)
    except Exception:
        return url
    if "tiktok" in p.netloc and p.query:
        url = p._replace(query="", fragment="").geturl()
    return url


def _resolve_short(url: str) -> str:
    """Follow a share short link (vt.tiktok.com, xhslink...) to the
    canonical page URL — resolvers and yt-dlp both do better with
    the real address. Best-effort: any failure keeps the original,
    and an IP-literal/localhost destination is never adopted."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return url
    if host not in _SHORT_HOSTS:
        return url
    try:
        import requests
        r = requests.get(
            url, headers={"User-Agent":
                          "Mozilla/5.0 (Linux; Android 13) "
                          "AppleWebKit/537.36"},
            timeout=15, allow_redirects=True)
        final = r.url or url
        fp = urlparse(final)
        if fp.scheme not in ("http", "https") or not fp.netloc:
            return url
        hn = (fp.hostname or "").lower()
        if not hn or hn == "localhost" \
                or re.match(r"^[\d.]+$", hn) \
                or re.match(r"^[0-9a-f:]+$", hn):
            return url
        return clean_url(final)
    except Exception:
        return url


class DownloadFailed(Exception):
    """Every download route failed (build 166, fix F1). str() is a
    polite Burmese customer message; .raw keeps the technical
    detail for the server log only — customers never see it."""

    def __init__(self, polite, raw=""):
        super().__init__(polite)
        self.polite = polite
        self.raw = raw


_POLITE = {
    "parse": "ဒီ link ကနေ video ကို ဆွဲလို့ မရသေးပါ။ "
             "Link အပြည့် ဖြစ်ရပါမယ် — Share → Copy link နဲ့ "
             "ပြန်ကူး (သို့) ခဏနေ ပြန်စမ်းပေးပါ။",
    "login": "ဒီ video က login ဝင်မှ ရတဲ့ video ဖြစ်နေပါတယ်။ "
             "Public video link နဲ့ ပြန်စမ်းပေးပါ။",
    "gone": "Video ကို မတွေ့တော့ပါ — ဖျက်ထားတာ (သို့) "
            "private လုပ်ထားတာ ဖြစ်နိုင်ပါတယ်။",
    "throttle": "တောင်းဆိုမှု များနေလို့ site က ခဏ "
                "ပိတ်ထားပါတယ်။ မိနစ် အနည်းငယ် စောင့်ပြီး "
                "ပြန်စမ်းပေးပါ။",
    "blocked": "ဒီ site က server ကနေ ဆွဲတာကို ပိတ်ထားပါတယ်။ "
               "ခဏနေ ပြန်စမ်းပေးပါ။",
    "network": "Network ပြဿနာ ဖြစ်နေပါတယ်။ "
               "ခဏနေ ပြန်စမ်းပေးပါ။",
    "default": "Download ဆွဲလို့ မရပါ။ Link ကို ပြန်စစ်ပြီး "
               "ပြန်စမ်းပေးပါ။",
}


def polite_error(err, url=""):
    """Map any download failure to its polite Burmese category
    (build 166, fix F1). The raw exception text stays server-side."""
    if isinstance(err, DownloadFailed):
        return err.polite
    s = (str(err) or "").lower()
    try:
        host = (urlparse(url).hostname or "").lower() if url else ""
    except Exception:
        host = ""
    if any(k in s for k in ("url parsing", "returned no video url",
                            "unsupported url", "not a valid url",
                            "no downloadable video",
                            "resolver returned")):
        cat = "parse"
    elif any(k in s for k in ("login", "sign in", "log in",
                              "cookies", "confirm you")) \
            or "facebook" in host or "instagram" in host:
        cat = "login"
    elif any(k in s for k in ("unavailable", "not found", "404",
                              "removed", "deleted", "private video",
                              "no longer available")):
        cat = "gone"
    elif "429" in s or "too many requests" in s \
            or "rate limit" in s or "rate-limit" in s:
        cat = "throttle"
    elif "403" in s or "blocked" in s or "forbidden" in s \
            or "unexpected response" in s:
        cat = "blocked"
    elif any(k in s for k in ("timed out", "timeout", "connection",
                              "network", "ssl", "certificate",
                              "getaddrinfo", "unreachable",
                              "eof occurred")):
        cat = "network"
    else:
        cat = "default"
    return _POLITE[cat]


def _pick_rendition(data, quality):
    """Choose which tikwm rendition URL to download (build 167).

    Returns (url, field_name) or (None, None). tikwm only includes
    the HD rendition (`hdplay`) when the request carries hd=1, and
    the pre-167 picker took `play` first even when hdplay existed —
    two stacked reasons TikTok jobs started from the standard
    rendition and came out blurry.
      hd  -> hdplay, then play, then wmplay (watermarked last)
      720 -> play (the standard no-watermark rendition), hdplay,
             then wmplay
      480 -> same order as 720: tikwm offers no 480 rendition, so
             the lightest no-watermark file wins."""
    if quality == "hd":
        order = ("hdplay", "play", "wmplay")
    else:
        order = ("play", "hdplay", "wmplay")
    for name in order:
        if data.get(name):
            return data[name], name
    return None, None


def _tiktok_fallback(url: str, workdir: Path, quality: str = "hd") -> dict:
    """Last-resort TikTok download via the public tikwm resolver API.

    TikTok increasingly serves datacenter IPs a stripped/blocker page, so
    even impersonated yt-dlp cannot extract the video. The resolver sees
    the video from its own (residential-grade) vantage point and returns
    a direct, watermark-free mp4 URL. No login/cookies involved.
    """
    import time as _t

    import requests

    headers = {"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36"}
    # hd=1 is load-bearing (build 167 diagnosis): without it tikwm
    # does not return the hdplay field AT ALL, so no picker could
    # ever choose the HD rendition. The payload is then a superset
    # and _pick_rendition decides per the job's quality.
    # Retry policy: one retry on a transport failure, and one retry
    # after a short pause when the resolver answers code -1 / no
    # rendition (its parse failures are sometimes transient). A
    # video it still cannot parse after that falls through to the
    # yt-dlp ladder in download().
    payload = None
    play = which = None
    last_exc = None
    for attempt in range(2):
        try:
            r = requests.get("https://www.tikwm.com/api/",
                             params={"url": url, "hd": 1},
                             headers=headers, timeout=30)
            r.raise_for_status()
            payload = r.json()
        except Exception as e:
            last_exc = e
            payload = None
        if payload:
            play, which = _pick_rendition(payload.get("data") or {},
                                          quality)
            if play:
                break
        if attempt == 0:
            _t.sleep(3)
    if not play:
        if payload is None and last_exc is not None:
            raise last_exc
        raise RuntimeError(f"tikwm resolver returned no video URL: {payload!r:.200}")
    data = payload.get("data") or {}
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
        "rendition": which,
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


def _xhs_fallback(url: str, workdir: Path, quality: str = "hd") -> dict:
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
        "format": _FORMAT_SELECTORS[quality],
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


def download(url: str, workdir: Path, quality: str = "hd") -> Path:
    ensure_system_ca()
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    # Build 166: normalise whatever was pasted (share text, short
    # link, tracking params) before any host check, so the
    # resolver, the ladder and the caption path all see the best
    # form of the URL.
    url = _resolve_short(clean_url(url))
    # Build 167: the job's quality choice picks the rendition class
    # on every route (resolver + ladder). Recorded in source_info
    # .json so the app can tell whether a cached preview matches
    # the quality a job asked for.
    quality = norm_quality(quality)
    out = workdir / "source.mp4"
    opts = {
        # Per-quality selectors (_FORMAT_SELECTORS): "720" keeps the
        # build-80 light renditions first — at the same resolution,
        # platforms offer several bitrates (TikTok's "download"
        # rendition of a 63 s clip is 39 MB where the standard one
        # is 9 MB); "hd" takes the best available, period.
        "format": _FORMAT_SELECTORS[quality],
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
            info = _xhs_fallback(url, workdir, quality)
        except Exception as e:
            first_err = e
    if info is None and "tiktok" in urlparse(url).netloc:
        # TikTok blocks this server's IP at the page level, so every
        # yt-dlp attempt below just burns minutes before failing. The
        # public resolver sees the video from a different vantage point
        # and is fast — try it FIRST; the ladder stays as the backup.
        try:
            info = _tiktok_fallback(url, workdir, quality)
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
        # Build 166 (fix F1): the customer gets a polite Burmese
        # category; the raw technical error goes to the server log
        # only, never to the phone screen or the Telegram chat.
        raw = str(first_err) if first_err else "unknown failure"
        print(f"[download] FAILED {url}: {raw[:400]}", flush=True)
        raise DownloadFailed(polite_error(first_err, url), raw)
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
        "quality": quality,
        "rendition": info.get("rendition"),
    }
    (workdir / "source_info.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


if __name__ == "__main__":
    path = download(sys.argv[1], Path(sys.argv[2]))
    print(path)
