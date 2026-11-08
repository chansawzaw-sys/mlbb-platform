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
"""


def _db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
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


def claim_admin(username):
    """One-shot: promote `username` to admin ONLY while no admin
    exists yet. Returns True if it happened."""
    with _LOCK, _db() as conn:
        if conn.execute("SELECT 1 FROM users WHERE role = 'admin'"
                        ).fetchone():
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
    with _LOCK, _db() as conn:
        conn.execute("UPDATE users SET disabled = ? WHERE id = ?",
                     (1 if disabled else 0, int(user_id)))
        if disabled:
            conn.execute("DELETE FROM sessions WHERE user_id = ?",
                         (int(user_id),))


def adjust_credits(user_id, delta):
    """Manual admin adjustment; the balance never goes negative."""
    with _LOCK, _db() as conn:
        row = conn.execute("SELECT credits FROM users WHERE id = ?",
                           (int(user_id),)).fetchone()
        if row is None:
            return None
        new = max(0, row["credits"] + int(delta))
        conn.execute("UPDATE users SET credits = ? WHERE id = ?",
                     (new, int(user_id)))
    return new


def set_password(user_id, password):
    if not password or len(password) < 6:
        return False
    with _LOCK, _db() as conn:
        conn.execute("UPDATE users SET pass_hash = ? WHERE id = ?",
                     (generate_password_hash(password), int(user_id)))
    return True


def link_tg(user_id, chat_id):
    with _LOCK, _db() as conn:
        conn.execute("UPDATE users SET tg_chat_id = ? WHERE id = ?",
                     (int(chat_id), int(user_id)))


def user_for_tg(chat_id):
    with _db() as conn:
        row = conn.execute("SELECT * FROM users WHERE tg_chat_id = ?",
                           (int(chat_id),)).fetchone()
    return _public(row)
