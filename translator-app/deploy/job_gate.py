#!/usr/bin/env python3
"""Deploy gate (build 246): is any translator job active right now?

Usage: job_gate.py <app-dir>

Reads the app's REAL on-disk job state — jobs/<id>/job.json under
the app dir, the same files the app itself writes — and exits:

  0  something is ACTIVE (a deploy must defer: change nothing)
  1  nothing active (a deploy may proceed)

Semantics (identical to the inline check apply-update.sh used
before this helper was factored out): a "running" job whose
job.json was touched within 25 minutes is live (running jobs
heartbeat the file); a "queued" job gets a longer 120-minute
window because it has no heartbeat yet. Anything else — finished
jobs, stale files, unreadable JSON — does not block a deploy.

Build 248: a Telegram bot wizard flow ALSO defers a deploy.
Build 246 made flows survive restarts (.tg_flows.json under the
app dir), but the 2026-11-24 build-247 update landed mid-flow
anyway: the flow state survived, yet the user's last tap / the
bot's next prompt fell in the restart seconds and the flow
appeared to hang. So the gate now also reads the persisted
flow store and defers while any flow is live. Liveness mirrors
the app's own load semantics (app.py _tg_flow_load): a flow is
live when now - its "ts" is within the 30-minute TTL (a
missing/unparseable ts reads as 0, hence expired) and any
source/logo file it references still exists. A missing or
unreadable store blocks nothing — the gate fails open toward
updating, exactly like the job check and the app's own silent
load. Stdlib only; never raises.
"""
import json
import sys
import time
from pathlib import Path


def job_active(app_dir):
    app = Path(app_dir)
    now = time.time()
    for p in app.glob("jobs/*/job.json"):
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
            age = now - p.stat().st_mtime
        except Exception:
            continue
        if j.get("status") == "running" and age < 25 * 60:
            return True
        if j.get("status") == "queued" and age < 120 * 60:
            return True
    return False


# Mirrors app.py _TG_FLOW_TTL (30 minutes, from each flow's "ts").
_FLOW_TTL_S = 30 * 60


def flow_active(app_dir):
    """True when the persisted bot wizard store (.tg_flows.json
    under the app dir) holds at least one LIVE flow, judged
    exactly as the app judges it on load (_tg_flow_load):
    not past the 30-minute expiry from its "ts", and its
    referenced src/logo_file (when set) still on disk.
    Missing/unreadable state never blocks (fail-open)."""
    p = Path(app_dir) / ".tg_flows.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    now = time.time()
    for f in data.values():
        try:
            if not isinstance(f, dict):
                continue
            if now - float(f.get("ts") or 0) > _FLOW_TTL_S:
                continue  # expired, as the app would drop it
            gone = False
            for key in ("src", "logo_file"):
                v = f.get(key)
                if v and not Path(str(v)).exists():
                    gone = True
                    break
            if gone:
                continue  # the app drops it at load too
            return True
        except Exception:
            continue
    return False


def main(argv):
    if len(argv) != 2:
        print("usage: job_gate.py <app-dir>")
        return 1
    if job_active(argv[1]):
        print("job_gate: active job present")
        return 0
    if flow_active(argv[1]):
        print("job_gate: active bot flow present")
        return 0
    print("job_gate: nothing active")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
