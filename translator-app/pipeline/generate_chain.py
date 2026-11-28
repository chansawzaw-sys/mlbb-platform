"""Script-generation chain (build 269) — the ONE walk every
prose generation in the app goes through: Recap scripts
(pipeline/recap.py), Story scripts + shot plans and the
Commentary scripts (both via pipeline/story.py's
_chain_generate seam, which delegates here).

Owner decision 2026-11-27, mirroring build 264 for segment
translation: generation is OpenAI-only. The pre-269 gateway
walk (Gemini lead while its probe verdict is green, then
OpenRouter -> xKiro, then Gemini's fallback slot, then
OpenAI) survives VERBATIM as _chain_generate_legacy below,
dormant behind the gate.

RE-ENABLE GATE: GENERATE_CHAIN=legacy in the project env
(.env or process env; also flippable from Admin -> the
OpenAI card, via /api/settings/generate-chain) restores
the legacy walk. The gate is read ONCE at import into this
module's constant _GEN_CHAIN, so a flip takes effect on
the next app start, never mid-job — the same rule as
build 264's TRANSLATE_CHAIN.

OpenAI mode calls ONLY pipeline/openai_translate
(generate_text: the gpt-4o-mini -> gpt-4.1-mini model
walk, Singapore-tunnel routed, 404 -> next model, 401
stops). No probe skip in OpenAI mode — the probe machinery
serves the legacy walk, and skipping the only engine on a
stale verdict would fail every script without an attempt
(build 264's rule, mirrored).

FAILURE SHAPE: if OpenAI fails (or answers junk the
caller's extract() rejects), chain_generate raises
RuntimeError with the failure detail and NO other engine
is tried — the callers wrap it in their polite Burmese
messages and the job fails loudly, never charged. There
is no silent fallback to the legacy engines while the
gate is on "openai".

Gemini TTS / voices are NOT part of this module and are
untouched: Gemini remains the voice engine; only prose
generation moved.
"""
import os

from . import (gemini_translate, openai_translate,
               openrouter_translate, translate_segments,
               xkiro_translate)
from .common import PROJECT_ROOT


# --- Build 269: the generation-chain gate -----------------
# "openai" (default): chain_generate calls ONLY
# pipeline/openai_translate. "legacy": the pre-269 gateway
# walk, preserved verbatim below. Read ONCE at import from
# the process env, falling back to the project .env — the
# same two sources, in the same order, as
# translate_segments._read_chain_gate (build 264). A flip
# therefore takes effect on the next app start, never
# mid-job.

def _read_generate_gate():
    v = (os.environ.get("GENERATE_CHAIN") or "").strip().lower()
    if not v:
        try:
            env_file = PROJECT_ROOT / ".env"
            if env_file.exists():
                for line in env_file.read_text(
                        encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("GENERATE_CHAIN="):
                        v = line.split("=", 1)[1].strip() \
                            .strip('"').strip("'").lower()
                        break
        except Exception:
            v = ""
    return "legacy" if v == "legacy" else "openai"


_GEN_CHAIN = _read_generate_gate()


def generate_mode():
    """The chain this process booted with: "openai" or
    "legacy" (surfaced by /api/settings/generate-chain so
    the Admin card can show what is actually live versus
    what is saved for the next start)."""
    return _GEN_CHAIN


def chain_generate(prompt, extract):
    """One generation: (payload, provider_label).

    `extract` maps a gateway's raw reply to the payload the
    caller wants, or None when the reply is junk (a junk
    reply fails the attempt, exactly like the pre-269
    in-walk usability gates). Raises RuntimeError with the
    per-engine detail when the active chain fails."""
    if _GEN_CHAIN == "legacy":
        return _chain_generate_legacy(prompt, extract)
    return _chain_generate_openai(prompt, extract)


def _chain_generate_openai(prompt, extract):
    """Build 269: OpenAI is the ONLY generation engine. No
    probe skip (see the module docstring). An unconfigured
    key or a failed/junk reply raises — never a silent
    substitution."""
    errors = {}
    if openai_translate.key_configured():
        try:
            payload = extract(
                openai_translate.generate_text(prompt) or "")
            if payload is not None:
                return payload, "openai"
            errors["openai"] = "unusable reply"
        except Exception as e:
            errors["openai"] = str(e)[:100]
    detail = "; ".join(f"{k}: {v}" for k, v in errors.items())
    raise RuntimeError(detail[:300] or "no gateway configured")


def _chain_generate_legacy(prompt, extract):
    """The pre-269 gateway walk, preserved verbatim (it was
    story._chain_generate's body, which itself carried
    recap's order and probe seams): Gemini leads while its
    probe verdict is green, then the free-first gateways
    OpenRouter -> xKiro, then Gemini's fallback slot, then
    OpenAI. Each reply goes through extract(); the first
    reply extract() accepts wins. Returns (payload,
    provider_label); raises RuntimeError with the
    per-gateway detail when every gateway failed or
    answered junk."""
    errors = {}

    def _try(name, fn, label_fn):
        try:
            payload = extract(fn(prompt) or "")
            if payload is not None:
                return payload, label_fn()
            errors[name] = "unusable reply"
        except Exception as e:
            errors[name] = str(e)[:100]
        return None

    gemini_tried = False
    if translate_segments._probe_engine_ok("gemini"):
        gemini_tried = True
        if gemini_translate.key_configured():
            r = _try("gemini", gemini_translate.generate_text,
                     lambda: "gemini")
            if r:
                return r
    if openrouter_translate.key_configured():
        r = _try("openrouter", openrouter_translate.generate_text,
                 openrouter_translate.last_label)
        if r:
            return r
    if xkiro_translate.key_configured():
        r = _try("xkiro", xkiro_translate.generate_text,
                 xkiro_translate.last_label)
        if r:
            return r
    if not gemini_tried \
            and not translate_segments._probe_blocked("gemini") \
            and gemini_translate.key_configured():
        r = _try("gemini", gemini_translate.generate_text,
                 lambda: "gemini")
        if r:
            return r
    if not translate_segments._probe_blocked("openai") \
            and openai_translate.key_configured():
        r = _try("openai", openai_translate.generate_text,
                 lambda: "openai")
        if r:
            return r
    detail = "; ".join(f"{k}: {v}" for k, v in errors.items())
    raise RuntimeError(detail[:300] or "no gateway configured")
