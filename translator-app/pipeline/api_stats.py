"""Per-provider translation usage counters for the Admin board.

One "video" is counted when a job's translation passes the
majority-translated gate in run_phase1 (build 138, owner request:
"which API translated how many videos"). The provider that produced
most of that job's segments gets the video count; every provider's
segment total is kept too, plus its last-used timestamp.

Stored in a server-side JSON file in the project root — it survives
deploys like .translate_probe.json and is never in git. Recording is
best-effort by design: a stats failure must never break a job.
"""
import json
import threading
import time
from collections import Counter

from .common import PROJECT_ROOT

_FILE = PROJECT_ROOT / ".api_stats.json"
_LOCK = threading.Lock()


def _load():
    try:
        return json.loads(_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def record(segments):
    """Count one translated job from its final segment list."""
    try:
        provs = [s.get("provider") for s in (segments or [])
                 if (s.get("text") or "").strip() and s.get("provider")]
        if not provs:
            return
        seg_counts = Counter(provs)
        dominant = seg_counts.most_common(1)[0][0]
        now = int(time.time())
        with _LOCK:
            d = _load()
            counts = d.setdefault("counts", {})
            segs = d.setdefault("segs", {})
            last = d.setdefault("last", {})
            counts[dominant] = counts.get(dominant, 0) + 1
            for name, n in seg_counts.items():
                segs[name] = segs.get(name, 0) + n
            last[dominant] = now
            d["total"] = d.get("total", 0) + 1
            tmp = _FILE.parent / (_FILE.name + ".tmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(_FILE)
    except Exception:
        pass


def read():
    d = _load()
    return {"counts": d.get("counts", {}), "segs": d.get("segs", {}),
            "last": d.get("last", {}), "total": d.get("total", 0)}


def reset():
    """Zero every counter (build 162, owner order: the produced-
    video numbers restart from 0). Atomic write, like record()."""
    with _LOCK:
        tmp = _FILE.parent / (_FILE.name + ".tmp")
        tmp.write_text("{}", encoding="utf-8")
        tmp.replace(_FILE)
    return read()
