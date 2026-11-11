"""Apps Script translator — Google's own infra, via the owner's account.

The owner deploys ~/workspace/apps-script-translator/AutoTranslate.gs as
a Web App (Execute as: Me, access: Anyone) and pastes its URL + WEB_SECRET
into the app's Admin card — never in chat. The app POSTs the segments as
JSON {secret, texts, source, target:"my"} and gets back
{ok:true, translations:[...]} translated by LanguageApp (Google
Translate, free, built-in).

Why this engine exists: the VPS IP is throttled by the free Google
endpoints, Gemini 404s (project lacks model access, build 136 probe)
and OpenAI 403s (VPS region blocked, build 136 probe). This route runs
under the OWNER's Google account on Google's servers, so the VPS IP
never touches Google Translate — the throttle that kills full jobs
does not apply here. Quality is standard Google Translate (faithful
and literal, not Gemini's narration style), so it sits AFTER Gemini
and OpenAI in the chain and BEFORE the throttled free chain: it is the
reliable fallback, not the style engine. Dub jobs keep their story
style only when Gemini/OpenAI answers; via Apps Script the dub text
is literal Google Myanmar.

Contract (same as gemini/openai_translate.py): translate_all takes a
list of dicts with 'text' and returns {index: burmese}. Response-level
failures raise so the caller falls through to the free chain; a single
bad item (empty, or an echo of the source) becomes "" in the result
so translate_segments._fill_missing can rescue it per-segment instead
of losing the whole batch. The caller's majority gate still applies,
and a failed job is still never charged.

Config handling (security): read ONLY from env APPS_SCRIPT_URL /
APPS_SCRIPT_SECRET or the git-ignored `.env` in the project root;
saved via the app's settings endpoint behind the admin login; the
secret is never returned in any API payload and never written to logs.
"""

import json
import os
import time
from pathlib import Path

from .common import PROJECT_ROOT

_TIMEOUT = 180  # Apps Script translates segment-by-segment server-side


def _env_get(name):
    val = (os.environ.get(name) or "").strip()
    if val:
        return val
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def get_url():
    return _env_get("APPS_SCRIPT_URL")


def get_secret():
    return _env_get("APPS_SCRIPT_SECRET")


def configured():
    return bool(get_url() and get_secret())


def _note_error(msg):
    try:
        (PROJECT_ROOT / ".apps_script_last_error").write_text(
            f"{int(time.time())} {msg}"[:400], encoding="utf-8")
    except Exception:
        pass


def _detect_source(texts):
    """One source language per request (the .gs doPost takes one).
    Each segment votes by its own dominant script — counting raw
    characters would let one long English line outvote several short
    Chinese ones. A tie defaults to zh-CN: the owner's jobs are
    Chinese drama, and that is also the .gs CONFIG default."""
    cjk_segs = latin_segs = 0
    for t in texts:
        cjk = sum(1 for ch in (t or "")
                  if "\u4e00" <= ch <= "\u9fff"
                  or "\u3040" <= ch <= "\u30ff")
        latin = sum(1 for ch in (t or "")
                    if ch.isalpha() and not
                    ("\u4e00" <= ch <= "\u9fff"
                     or "\u3040" <= ch <= "\u30ff"))
        if cjk >= latin and cjk > 0:
            cjk_segs += 1
        elif latin > 0:
            latin_segs += 1
    return "zh-CN" if cjk_segs >= latin_segs else "en"


def _post_batch(url, secret, texts, source):
    import requests

    resp = requests.post(url, json={
        "secret": secret, "texts": texts,
        "source": source, "target": "my",
    }, timeout=_TIMEOUT)
    if resp.status_code != 200:
        raise RuntimeError(f"apps-script HTTP {resp.status_code}")
    try:
        data = resp.json()
    except Exception:
        raise RuntimeError("apps-script returned non-JSON")
    if not data.get("ok"):
        raise RuntimeError(
            f"apps-script error: {str(data.get('error') or 'unknown')[:120]}")
    out = data.get("translations")
    if not isinstance(out, list) or len(out) != len(texts):
        raise RuntimeError(
            f"apps-script count mismatch: got "
            f"{len(out) if isinstance(out, list) else 'non-list'} "
            f"for {len(texts)}")
    return [str(t or "") for t in out]


def translate_all(segments, style="subs"):
    """segments: list of dicts with 'text'. Returns {index: burmese}.

    Style is accepted for the shared engine contract but ignored:
    LanguageApp has no narration mode (see module docstring).
    Raises on ANY response-level problem so the caller can fall back.
    """
    url, secret = get_url(), get_secret()
    if not url or not secret:
        raise RuntimeError("no Apps Script URL/secret configured")
    texts = [(s.get("text") or "").strip() for s in segments]
    source = _detect_source(texts)
    result = {}
    try:
        # Apps Script has a ~6-minute execution limit per call; keep
        # each POST to 80 segments so a long job stays inside it.
        for start in range(0, len(texts), 80):
            chunk = texts[start:start + 80]
            got = _post_batch(url, secret, chunk, source)
            for j, my in enumerate(got):
                my = (my or "").strip()
                # Echo (translation == source, source has letters) or
                # empty: leave "" so _fill_missing rescues the cue.
                if not my or (my == chunk[j] and any(
                        ch.isalpha() for ch in chunk[j])):
                    result[start + j] = ""
                else:
                    result[start + j] = my
    except Exception as e:
        _note_error(str(e)[:200])
        raise
    if not any(v for v in result.values()):
        msg = "apps-script returned no usable translations"
        _note_error(msg)
        raise RuntimeError(msg)
    return result


if __name__ == "__main__":
    print("apps-script configured:", configured(),
          "| url set:", bool(get_url()))
