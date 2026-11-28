"""Hard quota / billing walls (build 275): one classifier for
every provider the jobs call, so a HARD wall fails the job at
once with a clear Burmese reason instead of being retried
until a timeout.

The distinction this module exists to draw:

  * TRANSIENT rate limits (Gemini per-minute 429, a plain
    provider 429 with no quota signature) recover on their
    own — the existing retry / key-rotation behaviour stays
    exactly as it was, and this module returns None for them.
  * HARD walls do not recover inside a job's lifetime: the
    Gemini FREE-TIER DAILY quota (a 429 whose body names a
    per-day quota), a Gemini prepay balance at $0 (HTTP 402),
    ElevenLabs' paid-plan gate (HTTP 402 /
    paid_plan_required) or exhausted character quota
    (quota_exceeded), and OpenAI's insufficient_quota (the
    prepaid balance is gone). Retrying those only multiplies
    the wait — the 2026-11-19 dub that burned minutes walking
    dead Gemini keys at the voice stage is the incident this
    build answers.

`wall_message(provider, status, body_text)` returns the
Burmese customer-facing message when the response is a hard
wall, else None. Callers raise QuotaWallError with that
message; pipeline.parts treats the class as never-retry (like
the owner's own stop signals), and the app's _fail surfaces
the message verbatim with the usual zero-charge refund.
"""
import re


class QuotaWallError(RuntimeError):
    """A hard provider quota / billing wall. The message IS
    the customer-facing Burmese error (it names the cause and
    states that no credit was charged)."""


# The daily-quota signature inside a Gemini 429 body: the
# quotaId / quota_metric strings name the window
# ("GenerateRequestsPerDayPerProjectPerModel-FreeTier",
# "...PerDay...", "per day"). A per-minute wall says PerMinute
# instead and must NOT match — those stay transient.
_DAY_QUOTA_RE = re.compile(r"per[\s_\-]?day|requestsperday",
                           re.IGNORECASE)


def _is_day_quota(body_text):
    return bool(_DAY_QUOTA_RE.search(str(body_text or "")))


def wall_message(provider, status, body_text):
    """The Burmese fast-fail message when (status, body) is a
    hard quota/billing wall for `provider`, else None."""
    body = str(body_text or "")
    low = body.lower()
    if provider == "gemini":
        if status == 402:
            return ("Gemini credit ကုန်သွားပါပြီ — admin က "
                    "ကြိုတင်ငွေ (prepay) ပြန်ဖြည့်ပြီးမှ Gemini "
                    "အသံ ပြန်သုံးလို့ရပါမယ် (credit လုံးဝ မဖြတ်ပါ)")
        if status == 429 and _is_day_quota(body):
            return ("Gemini ရဲ့ ဒီနေ့ free quota ကုန်သွားပါပြီ — "
                    "နောက်နေ့မှ ပြန်စမ်းပါ၊ ဒါမှမဟုတ် တခြား အသံ "
                    "ရွေးပါ (credit လုံးဝ မဖြတ်ပါ)")
        return None
    if provider == "elevenlabs":
        if status == 402 or "paid_plan_required" in low \
                or "quota_exceeded" in low:
            return ("ElevenLabs plan/quota ပြည့်သွားပါပြီ — admin က "
                    "paid plan စစ်ပြီးမှ ဒီ အသံ ပြန်သုံးလို့ရပါမယ် "
                    "(credit လုံးဝ မဖြတ်ပါ)")
        return None
    if provider == "openai":
        if "insufficient_quota" in low:
            return ("OpenAI credit ကုန်သွားပါပြီ — admin က credit "
                    "ပြန်ဖြည့်ပြီးမှ ပြန်စမ်းပါ "
                    "(credit လုံးဝ မဖြတ်ပါ)")
        return None
    return None
