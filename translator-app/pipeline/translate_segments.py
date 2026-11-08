"""Step 3: translate each segment to Myanmar (Burmese).

Provider chain:
  0. Gemini Flash (only when GEMINI_API_KEY is configured server-side —
     see pipeline/gemini_translate.py): ONE request for ALL segments,
     natural spoken Burmese. Any failure -> fall through to the free chain.
  1. deep-translator GoogleTranslator (unofficial Google Translate endpoint)
  2. `translators` library -> Microsoft Bing Translator web endpoint
  3. keep the source text (never drop a cue)

Why a chain: from this sandbox's shared egress IP, Google Translate answers
with its "sorry / too many requests" page (verified 2026-11-07) and MyMemory's
free daily quota for the IP was already exhausted, while Bing worked and
returned correct Burmese. On a normal residential IP, provider 1 usually works.

CLI: python -m pipeline.translate_segments <workdir>
Reads segments_en.json, writes segments_my.json + subs_my.srt (original timings).
"""
import sys
import time
from pathlib import Path

from .common import ensure_system_ca, load_segments, save_segments, write_srt


def _google(text):
    from deep_translator import GoogleTranslator
    return GoogleTranslator(source="auto", target="my").translate(text)


def _bing(text):
    import translators as ts
    return ts.translate_text(text, translator="bing",
                             from_language="en", to_language="my")


_PROVIDERS = [("google", _google), ("bing", _bing)]


def translate_one(text):
    """Return (burmese_text, provider_name)."""
    last_err = None
    for name, fn in _PROVIDERS:
        for attempt in range(2):
            try:
                out = (fn(text) or "").strip()
                if out:
                    return out, name
            except Exception as e:
                last_err = e
                time.sleep(1.0 * (attempt + 1))
    return text, f"fallback-source ({last_err})"


def translate_segments(workdir):
    ensure_system_ca()
    workdir = Path(workdir)
    segs = load_segments(workdir / "segments_en.json")
    # 0. Gemini batch — first choice when a key is configured.
    if segs:
        try:
            from . import gemini_translate
            if gemini_translate.key_configured():
                by_idx = gemini_translate.translate_all(segs)
                out = [{**seg, "text_my": by_idx[i], "provider": "gemini"}
                       for i, seg in enumerate(segs)]
                save_segments(out, workdir / "segments_my.json")
                write_srt(out, workdir / "subs_my.srt")
                return out
        except Exception:
            pass  # any Gemini failure -> free chain below, as designed
    out = []
    for seg in segs:
        text_my, provider = translate_one(seg["text"])
        out.append({**seg, "text_my": text_my, "provider": provider})
    save_segments(out, workdir / "segments_my.json")
    write_srt(out, workdir / "subs_my.srt")
    return out


if __name__ == "__main__":
    res = translate_segments(Path(sys.argv[1]))
    provs = {}
    for r in res:
        provs[r["provider"]] = provs.get(r["provider"], 0) + 1
    print(f"{len(res)} segments translated, providers: {provs}")
