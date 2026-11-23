#!/usr/bin/env python3
"""Deploy gate (build 246): is any translator job active right now?

Usage: job_gate.py <app-dir>

Reads the app's REAL on-disk job state — jobs/<id>/job.json under
the app dir, the same files the app itself writes — and exits:

  0  a job is ACTIVE (a deploy must defer: change nothing)
  1  no active job (a deploy may proceed)

Semantics (identical to the inline check apply-update.sh used
before this helper was factored out): a "running" job whose
job.json was touched within 25 minutes is live (running jobs
heartbeat the file); a "queued" job gets a longer 120-minute
window because it has no heartbeat yet. Anything else — finished
jobs, stale files, unreadable JSON — does not block a deploy.
Stdlib only; never raises.
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


def main(argv):
    if len(argv) != 2:
        print("usage: job_gate.py <app-dir>")
        return 1
    active = job_active(argv[1])
    print("job_gate: active job present" if active
          else "job_gate: no active job")
    return 0 if active else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
