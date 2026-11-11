"""Web accounts for the translator (commercial stage).

SQLite (stdlib) in the app dir: users.db — survives the file-copy
deploys, never in git. Passwords are werkzeug hashes (scrypt); the
plaintext is never stored or logged. Sessions are server-side rows
with revocable random tokens. Login failures are counted per
username+IP and lock the pair for a while after too many misses.

Roles: "user" sees only their own jobs; "admin" sees and edits
everything. There is exactly one bootstrap path for the first admin:
claim_admin() works only while NO admin exists (the app still sits
behind the owner's login wall at that point) and never again after.
"""
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

DB_PATH = Path(__file__).resolve().parent.parent / "users.db"
_LOCK = threading.Lock()
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.]{3,24}$")
_MAX_FAILS = 5
_LOCK_SECONDS = 15 * 60

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  pass_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user',
  credits INTEGER NOT NULL DEFAULT 0,
  disabled INTEGER NOT NULL DEFAULT 0,
  tg_chat_id INTEGER UNIQUE,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS login_fails (
  key TEXT PRIMARY KEY,
  count INTEGER NOT NULL,
  locked_until REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  delta INTEGER NOT NULL,
  reason TEXT NOT NULL,
  ref TEXT,
  ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS topups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  credits INTEGER NOT NULL,
  proof TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at REAL NOT NULL,
  decided_at REAL
);
CREATE TABLE IF NOT EXISTS deleted_users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT,
  created_at REAL,
  deleted_at REAL,
  why TEXT
);
"""


def _db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
    if "last_login" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN last_login REAL")
    if "has_job" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN has_job INTEGER DEFAULT 0")
    return conn


def _public(row):
    if row is None:
        return None
    return {"id": row["id"], "username": row["username"],
            "role": row["role"], "credits": row["credits"],
            "disabled": bool(row["disabled"]),
            "tg_chat_id": row["tg_chat_id"],
            "created_at": row["created_at"]}


def create_user(username, password):
    """(user, error) — usernames unique case-insensitively."""
    username = (username or "").strip()
    if not _USERNAME_RE.match(username):
        return None, "username ၃–၂၄ လုံး, အင်္ဂလိပ်/ဂဏန်း/./_ ပဲ ရပါတယ်"
    if not password or len(password) < 6:
        return None, "password အနည်းဆုံး ၆ လုံး ထားပါ"
    with _LOCK, _db() as conn:
        if conn.execute("SELECT 1 FROM users WHERE username = ?",
                        (username,)).fetchone():
            return None, "ဒီ username ယူပြီးသား ဖြစ်နေပါတယ်"
        cur = conn.execute(
            "INSERT INTO users (username, pass_hash, created_at) "
            "VALUES (?, ?, ?)",
            (username, generate_password_hash(password), time.time()))
        row = conn.execute("SELECT * FROM users WHERE id = ?",
                           (cur.lastrowid,)).fetchone()
    return _public(row), None


def _fail_key(username, ip):
    return f"{(username or '').lower()}|{ip or ''}"


def login_locked(username, ip):
    with _db() as conn:
        row = conn.execute("SELECT * FROM login_fails WHERE key = ?",
                           (_fail_key(username, ip),)).fetchone()
    return bool(row and row["locked_until"] > time.time())


def _note_fail(username, ip):
    key = _fail_key(username, ip)
    with _LOCK, _db() as conn:
        row = conn.execute("SELECT * FROM login_fails WHERE key = ?",
                           (key,)).fetchone()
        count = (row["count"] if row else 0) + 1
        locked = time.time() + _LOCK_SECONDS if count >= _MAX_FAILS else 0
        conn.execute(
            "INSERT INTO login_fails (key, count, locked_until) "
            "VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
            "count = excluded.count, locked_until = excluded.locked_until",
            (key, count, locked))


def _clear_fails(username, ip):
    with _LOCK, _db() as conn:
        conn.execute("DELETE FROM login_fails WHERE key = ?",
                     (_fail_key(username, ip),))


def authenticate(username, password, ip=""):
    """(user, error). Lockout checked first; failures are counted."""
    username = (username or "").strip()
    if login_locked(username, ip):
        return None, "ခဏ ခဏ မှားထားလို့ ၁၅ မိနစ် ပိတ်ထားပါတယ်"
    with _db() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?",
                           (username,)).fetchone()
    if (not row or not check_password_hash(row["pass_hash"],
                                           password or "")):
        _note_fail(username, ip)
        return None, "username သို့မဟုတ် password မှားနေပါတယ်"
    if row["disabled"]:
        return None, "ဒီ account ပိတ်ထားပါတယ် — admin ကို ဆက်သွယ်ပါ"
    _clear_fails(username, ip)
    with _LOCK, _db() as conn:
        conn.execute("UPDATE users SET last_login = ? WHERE id = ?",
                     (time.time(), row["id"]))
    return _public(row), None


def new_session(user_id, days=30):
    token = secrets.token_urlsafe(32)
    with _LOCK, _db() as conn:
        conn.execute("DELETE FROM sessions WHERE expires < ?",
                     (time.time(),))
        conn.execute("INSERT INTO sessions (token, user_id, expires) "
                     "VALUES (?, ?, ?)",
                     (token, user_id, time.time() + days * 86400))
    return token


def user_for_token(token):
    if not token:
        return None
    with _db() as conn:
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token = ? AND s.expires > ?",
            (token, time.time())).fetchone()
    if row is None or row["disabled"]:
        return None
    return _public(row)


def revoke(token):
    with _LOCK, _db() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def admin_counts():
    """(total admin accounts, enabled admin accounts)."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT disabled FROM users WHERE role = 'admin'").fetchall()
    return len(rows), sum(1 for r in rows if not r["disabled"])


def list_admins():
    """All admin accounts (public dicts) — used by the bot-side
    lockout recovery (build 158)."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM users WHERE role = 'admin' "
            "ORDER BY id").fetchall()
    return [_public(r) for r in rows]


def claim_admin(username, recovery_tg_id=None):
    """One-shot: promote `username` to admin ONLY while no admin
    exists yet. Returns True if it happened.

    Recovery (build 157): if admin accounts exist but EVERY one is
    disabled — the owner locked themselves out before the
    self-disable guard existed — the one account linked to the
    owner's fixed Telegram chat may claim instead. Nobody else can:
    a working admin existing always closes the claim."""
    with _LOCK, _db() as conn:
        admins = conn.execute(
            "SELECT disabled FROM users WHERE role = 'admin'").fetchall()
        if admins:
            if any(not r["disabled"] for r in admins):
                return False
            if not recovery_tg_id:
                return False
            me = conn.execute(
                "SELECT tg_chat_id FROM users WHERE username = ?",
                (username,)).fetchone()
            if (me is None or me["tg_chat_id"] is None
                    or str(me["tg_chat_id"]) != str(recovery_tg_id)):
                return False
        cur = conn.execute(
            "UPDATE users SET role = 'admin' WHERE username = ?",
            (username,))
        return cur.rowcount == 1


def search_users(query, limit=20):
    """Admin lookup — SEARCH-FIRST by design: an empty/short query
    returns nothing, never a dump of every account."""
    q = (query or "").strip()
    if len(q) < 2:
        return []
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM users WHERE username LIKE ? ESCAPE '\\' "
            "ORDER BY username LIMIT ?",
            ("%" + q.replace("\\", "\\\\").replace("%", "\\%")
             .replace("_", "\\_") + "%", int(limit))).fetchall()
    return [_public(r) for r in rows]


def get_user(user_id):
    with _db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?",
                           (int(user_id),)).fetchone()
    return _public(row)


def set_disabled(user_id, disabled):
    """Enable/disable an account (disabling kills its sessions).

    An ADMIN account can never be disabled (build 157): only an
    admin could re-enable it, so disabling one is a one-way lockout
    of the whole shop. Returns False when refused."""
    with _LOCK, _db() as conn:
        row = conn.execute("SELECT role FROM users WHERE id = ?",
                           (int(user_id),)).fetchone()
        if row is None:
            return False
        if disabled and row["role"] == "admin":
            return False
        conn.execute("UPDATE users SET disabled = ? WHERE id = ?",
                     (1 if disabled else 0, int(user_id)))
        if disabled:
            conn.execute("DELETE FROM sessions WHERE user_id = ?",
                         (int(user_id),))
    return True


def adjust_credits(user_id, delta):
    """Manual admin adjustment — ledger-backed like every other
    credit move (the old version updated the balance with NO ledger
    row and silently clamped at zero, so the audit trail had holes
    exactly where manual moves happened). A move that would take the
    balance below zero is refused whole (returns None)."""
    return apply_credit(user_id, int(delta), "admin-adjust")


def mark_has_job(user_id):
    """Sticky flag: this account has submitted at least one job.
    The dormant sweep trusts this (plus the caller's live job list),
    not just job folders still on disk — those are deleted 15 min /
    24 h after finishing, long before the 60-day sweep runs."""
    if not user_id:
        return
    with _LOCK, _db() as conn:
        conn.execute("UPDATE users SET has_job = 1 WHERE id = ?",
                     (int(user_id),))


def set_password(user_id, password):
    """Set a new password AND revoke every existing session for
    the account: a password change/reset is exactly the moment
    other logins must die (audit C7). The caller may mint a fresh
    session for the device that made the change."""
    if not password or len(password) < 6:
        return False
    with _LOCK, _db() as conn:
        conn.execute("UPDATE users SET pass_hash = ? WHERE id = ?",
                     (generate_password_hash(password), int(user_id)))
        conn.execute("DELETE FROM sessions WHERE user_id = ?",
                     (int(user_id),))
    return True


def link_tg(user_id, chat_id):
    """Link a Telegram chat to an account. tg_chat_id is UNIQUE:
    a chat already linked to ANOTHER account is a clean refusal
    (False), never an IntegrityError 500 (audit B6)."""
    try:
        with _LOCK, _db() as conn:
            conn.execute("UPDATE users SET tg_chat_id = ? WHERE id = ?",
                         (int(chat_id), int(user_id)))
        return True
    except sqlite3.IntegrityError:
        return False


def user_for_tg(chat_id):
    with _db() as conn:
        row = conn.execute("SELECT * FROM users WHERE tg_chat_id = ?",
                           (int(chat_id),)).fetchone()
    return _public(row)


# ---------------- credits: ledger, charges, top-ups ----------------

def apply_credit(user_id, delta, reason, ref=None):
    """One ledger-backed balance move. A negative move that would
    take the balance below zero is refused whole (returns None) —
    charges are all-or-nothing, never partial. Returns the new
    balance on success."""
    with _LOCK, _db() as conn:
        row = conn.execute("SELECT credits FROM users WHERE id = ?",
                           (int(user_id),)).fetchone()
        if row is None:
            return None
        new = row["credits"] + int(delta)
        if new < 0:
            return None
        conn.execute("UPDATE users SET credits = ? WHERE id = ?",
                     (new, int(user_id)))
        conn.execute(
            "INSERT INTO ledger (user_id, delta, reason, ref, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (int(user_id), int(delta), reason, ref, time.time()))
    return new


def charge(user_id, amount, ref):
    """Charge `amount` credits for a job; False when insufficient."""
    if amount <= 0:
        return True
    return apply_credit(user_id, -int(amount), "job", ref) is not None


def refund(user_id, amount, ref):
    if amount > 0:
        apply_credit(user_id, int(amount), "refund", ref)


def ledger_for(user_id, limit=20):
    with _db() as conn:
        rows = conn.execute(
            "SELECT delta, reason, ref, ts FROM ledger "
            "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (int(user_id), int(limit))).fetchall()
    return [dict(r) for r in rows]


def create_topup(user_id, credits, proof):
    with _LOCK, _db() as conn:
        cur = conn.execute(
            "INSERT INTO topups (user_id, credits, proof, created_at) "
            "VALUES (?, ?, ?, ?)",
            (int(user_id), int(credits), proof, time.time()))
        return cur.lastrowid


def set_topup_proof(topup_id, proof):
    with _LOCK, _db() as conn:
        conn.execute("UPDATE topups SET proof = ? WHERE id = ?",
                     (proof, int(topup_id)))


def get_topup(topup_id):
    with _db() as conn:
        row = conn.execute(
            "SELECT t.*, u.username, u.tg_chat_id FROM topups t "
            "JOIN users u ON u.id = t.user_id WHERE t.id = ?",
            (int(topup_id),)).fetchone()
    return dict(row) if row else None


def pending_topups():
    with _db() as conn:
        rows = conn.execute(
            "SELECT t.*, u.username FROM topups t "
            "JOIN users u ON u.id = t.user_id "
            "WHERE t.status = 'pending' ORDER BY t.id").fetchall()
    return [dict(r) for r in rows]


def topups_for(user_id, limit=10):
    """One customer's own recent top-up requests (web app status
    list, build 156) — never exposes other users' rows."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, credits, status, created_at, decided_at "
            "FROM topups WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (int(user_id), int(limit))).fetchall()
    return [dict(r) for r in rows]


def delete_user(user_id, why="manual"):
    """Hard-delete an account (sessions die with it) and leave an
    archive row. Admin accounts can never be deleted this way.
    The credit ledger is kept — it is the money audit trail."""
    with _LOCK, _db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?",
                           (int(user_id),)).fetchone()
        if row is None or row["role"] == "admin":
            return False
        conn.execute(
            "INSERT INTO deleted_users (username, created_at, "
            "deleted_at, why) VALUES (?, ?, ?, ?)",
            (row["username"], row["created_at"], time.time(), why))
        conn.execute("DELETE FROM sessions WHERE user_id = ?",
                     (int(user_id),))
        conn.execute("DELETE FROM users WHERE id = ?", (int(user_id),))
    return True


def dormant_users(days=60, exclude_ids=()):
    """Dead signups: no admin role, zero credits, and no login (or
    registration, when never logged in) for `days` days. Callers
    additionally exclude accounts that ever ran a job."""
    cutoff = time.time() - days * 86400
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM users WHERE role != 'admin' AND credits = 0 "
            "AND COALESCE(has_job, 0) = 0 "
            "AND COALESCE(last_login, created_at) < ?",
            (cutoff,)).fetchall()
    return [_public(r) for r in rows
            if r["id"] not in set(exclude_ids or ())]


def decide_topup(topup_id, ok):
    """Admin decision, exactly-once: the status guard means a second
    tap (or a second admin surface) can never credit twice. Returns
    {user_id, credits, tg_chat_id} when this call decided it.
    APPROVAL additionally requires a proof screenshot on file: a
    top-up whose proof download failed must never be creditable
    sight-unseen (audit A10) — rejection stays possible."""
    with _LOCK, _db() as conn:
        if ok:
            prow = conn.execute(
                "SELECT proof FROM topups WHERE id = ?",
                (int(topup_id),)).fetchone()
            if prow is None or not (prow["proof"] or "").strip():
                return None
        cur = conn.execute(
            "UPDATE topups SET status = ?, decided_at = ? "
            "WHERE id = ? AND status = 'pending'",
            ("approved" if ok else "rejected", time.time(),
             int(topup_id)))
        if cur.rowcount != 1:
            return None
        row = conn.execute(
            "SELECT t.credits, t.user_id, u.tg_chat_id FROM topups t "
            "JOIN users u ON u.id = t.user_id WHERE t.id = ?",
            (int(topup_id),)).fetchone()
    if ok:
        apply_credit(row["user_id"], row["credits"], "topup",
                     f"topup:{topup_id}")
    return {"user_id": row["user_id"], "credits": row["credits"],
            "tg_chat_id": row["tg_chat_id"]}
