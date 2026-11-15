"""Shared .env read-modify-write — build 192 root fix.

Every Admin-card save in this app edits exactly ONE key of the
project .env (an API key, the bot token, the tunnel bookkeeping,
the payment text...). Until build 192 every writer did its own
unguarded whole-file read-modify-write: read the file, drop the
lines for its key, append the new line, `write_text` the result
back — truncating the file in place, with no lock and no atomic
replace. The writers were: app.py's `_env_write` / `_env_remove`,
the eight near-identical inline copies in the key-card handlers,
pipeline/tunnel.py's `_env_set`, and (deploy side)
activate-settings.sh's grep+`cat >` setters.

Individually each looks harmless; together they lose keys. The
app is served threaded (serve.py: `threaded=True`) and the
deploy activation rewrites the pointer keys on its own schedule,
so two writers routinely overlap, and BOTH overlap shapes lose
data:

  * stale-snapshot overwrite — writer A reads the file, writer B
    reads+writes its own key, writer A writes its (stale)
    snapshot back: B's key is silently gone;
  * truncated-read snapshot — writer A reads while writer B's
    in-place rewrite has truncated the file but not yet flushed
    the new content: A snapshots a partial file (every line not
    yet written is missing) and later persists that snapshot,
    making the loss permanent.

That is how TG_API_ID + TG_API_HASH — saved on 2026-11-14,
still readable by the activation on 2026-11-15 ~09:22 — were
gone by ~09:56, across a morning of tunnel-card saves and
activation runs: the tunnel card's six sequential whole-file
rewrites and the activation's pointer rewrites kept racing, and
one truncated-read snapshot stuck.

This module is the single implementation every app-side writer
now funnels through:

  * FRESH READ at write time — the file is re-read inside the
    lock, never from a cached snapshot;
  * FOREIGN LINES PRESERVED BYTE-FOR-BYTE — lines are split on
    "\\n" only (read with newline=""), so comments, blank lines,
    quoted values, CRLF endings and keys nobody owns (TG_API_*,
    TG_API_BASE, VT_*, anything future) pass through untouched;
    only lines starting with the writer's own `KEY=` are dropped;
  * LOCKED — an flock on `.env.lock` next to the .env serialises
    writers across threads AND processes (each call opens its
    own fd, so threads genuinely contend); the deploy-side shell
    setters take the same flock;
  * ATOMIC — the new content lands via a temp file in the same
    directory + os.replace, so a concurrent reader sees the old
    file or the new file, never a truncated one.

Readers are unchanged (they were never the problem).
"""
import contextlib
import fcntl
import os
import tempfile
from pathlib import Path


def lock_path_for(env_path):
    env_path = Path(env_path)
    return env_path.parent / (env_path.name + ".lock")


@contextlib.contextmanager
def _locked(env_path):
    """Exclusive flock on `.env.lock` (created if needed). One
    open file description per call, so threads of this process
    serialise against each other exactly like processes do."""
    fd = os.open(lock_path_for(env_path),
                 os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _read_lines(env_path):
    """The file as a list of lines WITHOUT their "\\n" (a line's
    own trailing "\\r" is kept, so CRLF foreign lines survive
    byte-for-byte). A missing file reads as no lines."""
    try:
        with open(env_path, "r", encoding="utf-8", newline="") as f:
            raw = f.read()
    except FileNotFoundError:
        return []
    parts = raw.split("\n")
    if parts and parts[-1] == "":
        parts.pop()  # artefact of the trailing newline
    return parts


def _atomic_write(env_path, text):
    fd, tmp = tempfile.mkstemp(dir=str(env_path.parent),
                               prefix=env_path.name + ".tmp.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, env_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def update(env_path, key, value):
    """Set `key` to `value`, or remove the key when `value` is
    None. Every other line of the file is preserved
    byte-for-byte; the file is re-read fresh inside the lock;
    the write itself is atomic. The result always ends with a
    trailing newline (or is empty when no lines remain) — the
    exact shape the pre-192 writers produced."""
    env_path = Path(env_path)
    with _locked(env_path):
        kept = [ln for ln in _read_lines(env_path)
                if not ln.startswith(key + "=")]
        if value is not None:
            kept.append(f"{key}={value}")
        text = "\n".join(kept) + ("\n" if kept else "")
        _atomic_write(env_path, text)
