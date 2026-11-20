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
import hashlib
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
CREATE TABLE IF NOT EXISTS clones (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  voice_id TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS cover_series (
  user_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  name_key TEXT NOT NULL,
  last_part INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL,
  PRIMARY KEY (user_id, name_key)
);
CREATE TABLE IF NOT EXISTS app_meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS voice_favs (
  user_id INTEGER NOT NULL,
  voice_id TEXT NOT NULL,
  created_at REAL NOT NULL,
  PRIMARY KEY (user_id, voice_id)
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
    # Build 180: Google sign-in. google_sub is the stable Google
    # account id; the partial unique index keeps it unique among
    # linked accounts while any number of password-only rows keep
    # NULL. email is the address Google verified for that sub.
    if "google_sub" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN google_sub TEXT")
    if "email" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN email TEXT")
    # Build 187: Google-born accounts owe a one-time username +
    # password setup before they may start jobs. Only rows created
    # by create_google_user ever carry 1; password registrations
    # and linked accounts keep the default 0 forever.
    if "needs_setup" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN needs_setup "
                     "INTEGER DEFAULT 0")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_sub "
        "ON users(google_sub) WHERE google_sub IS NOT NULL")
    # Build 221: referral system. Every account owns a unique,
    # stable referral_code; referred_by points at the inviter's
    # user id; referral_rewarded is the exactly-once flag the
    # inviter's reward flips (see pay_referral_reward). Existing
    # accounts are backfilled here — the UPDATE guards on the
    # row still being codeless and a code collision just retries
    # with a fresh code, so concurrent first touches are safe.
    if "referral_code" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN referral_code "
                     "TEXT")
    if "referred_by" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN referred_by "
                     "INTEGER")
    if "referral_rewarded" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN "
                     "referral_rewarded INTEGER DEFAULT 0")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_referral_code "
        "ON users(referral_code) WHERE referral_code IS NOT NULL")
    # Build 224: reseller API token. api_token_hash holds ONLY
    # the SHA-256 of the account's one API token — the plaintext
    # is shown to the owner exactly once at generation and never
    # stored, so a database read can never leak a usable token.
    # The partial unique index keeps one token per account and
    # token -> account resolution unambiguous.
    if "api_token_hash" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN "
                     "api_token_hash TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_api_token "
        "ON users(api_token_hash) WHERE api_token_hash IS NOT NULL")
    tcols = [r[1] for r in conn.execute(
        "PRAGMA table_info(topups)")]
    if "package_kyat" not in tcols:
        conn.execute("ALTER TABLE topups ADD COLUMN "
                     "package_kyat INTEGER")
    # Build 231: THE CREDIT RESCALE. One credit was 150 kyat;
    # from this build 1 credit = 5 kyat (owner's BV-style
    # pricing), so every balance and every pending amount is
    # re-expressed x30 and kyat prices hold exactly. Runs ONCE
    # (app_meta marker), atomically: balances convert through
    # APPEND-ONLY ledger entries (history is never edited —
    # each non-zero balance gains one 'rescale-231' entry of
    # 29x, bringing its ledger sum to the new balance), pending
    # topups convert with them (decided rows are history and
    # stay untouched), then the marker is set. A crash anywhere
    # rolls the whole conversion back — a half-rescaled ledger
    # can never ship.
    if conn.execute("SELECT 1 FROM app_meta WHERE key = ?",
                    ("rescale_231",)).fetchone() is None:
        # Rate keys the owner may have saved are still in OLD
        # units; rewrite any PRESENT key to the approved rate
        # card first (idempotent values, outside the money
        # transaction, so a crash simply retries next boot).
        try:
            from . import envfile as _envfile
            _env_path = DB_PATH.parent / ".env"
            _card = {"ELEVENLABS_CREDITS_PER_MIN": "100",
                     "GEMINI_CREDITS_PER_MIN": "80",
                     "COMMENTARY_CREDITS_PER_MIN": "80",
                     "CAPTIONS_CREDITS_PER_MIN": "20",
                     "REFERRAL_REWARD_CREDITS": "60"}
            _present = set()
            if _env_path.exists():
                for _ln in _env_path.read_text(
                        encoding="utf-8").splitlines():
                    if "=" in _ln:
                        _present.add(
                            _ln.split("=", 1)[0].strip())
            for _k, _v in _card.items():
                if _k in _present:
                    _envfile.update(_env_path, _k, _v)
        except Exception:
            pass
        for _u in conn.execute(
                "SELECT id, credits FROM users "
                "WHERE credits != 0").fetchall():
            conn.execute(
                "INSERT INTO ledger (user_id, delta, reason, "
                "ref, ts) VALUES (?, ?, 'rescale-231', ?, ?)",
                (_u["id"], _u["credits"] * 29,
                 "credit 150 kyat -> 5 kyat (x30)",
                 time.time()))
            conn.execute(
                "UPDATE users SET credits = credits * 30 "
                "WHERE id = ?", (_u["id"],))
        conn.execute(
            "UPDATE topups SET credits = credits * 30 "
            "WHERE status = 'pending'")
        conn.execute(
            "INSERT INTO app_meta (key, value) "
            "VALUES ('rescale_231', 'x30')")
        conn.commit()
    missing = conn.execute(
        "SELECT id FROM users WHERE referral_code IS NULL").fetchall()
    for r in missing:
        for _try in range(10):
            try:
                conn.execute(
                    "UPDATE users SET referral_code = ? "
                    "WHERE id = ? AND referral_code IS NULL",
                    (_gen_referral_code(), r["id"]))
            except sqlite3.IntegrityError:
                continue  # code taken by a racing insert; retry
            break  # filled — or a racing fill got there first
    if missing:
        conn.commit()
    return conn


# Referral codes (build 221): 8 chars from an alphabet with no
# look-alikes (no 0/O, 1/I/L) so a code survives being read off
# a screen and typed by hand.
_REF_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _gen_referral_code():
    return "".join(secrets.choice(_REF_ALPHABET) for _ in range(8))


def _fresh_referral_code(conn):
    """A code no account owns yet (caller holds _LOCK)."""
    for _try in range(20):
        code = _gen_referral_code()
        if not conn.execute(
                "SELECT 1 FROM users WHERE referral_code = ?",
                (code,)).fetchone():
            return code
    raise RuntimeError("referral code generation failed")


def _public(row):
    if row is None:
        return None
    keys = row.keys()
    return {"id": row["id"], "username": row["username"],
            "role": row["role"], "credits": row["credits"],
            "disabled": bool(row["disabled"]),
            "tg_chat_id": row["tg_chat_id"],
            "email": row["email"] if "email" in keys else None,
            "google_linked": bool("google_sub" in keys
                                  and row["google_sub"]),
            "needs_setup": bool("needs_setup" in keys
                                and row["needs_setup"]),
            "referral_code": row["referral_code"]
            if "referral_code" in keys else None,
            "referred_by": row["referred_by"]
            if "referred_by" in keys else None,
            "created_at": row["created_at"]}


def create_user(username, password, referred_by=None):
    """(user, error) — usernames unique case-insensitively.
    Build 221: every new account is born with its own referral
    code; `referred_by` (the inviter's user id, already resolved
    from a code by the caller) is stamped in the same insert."""
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
            "INSERT INTO users (username, pass_hash, created_at, "
            "referral_code, referred_by) VALUES (?, ?, ?, ?, ?)",
            (username, generate_password_hash(password), time.time(),
             _fresh_referral_code(conn),
             int(referred_by) if referred_by else None))
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
            "UPDATE users SET role = 'admin', needs_setup = 0 "
            "WHERE username = ?",
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
    not just job folders still on disk — those are deleted 24 h
    after finishing, long before the 60-day sweep runs."""
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


# ---------------- Google sign-in (build 180) ----------------

def user_for_google_sub(sub):
    if not sub:
        return None
    with _db() as conn:
        row = conn.execute("SELECT * FROM users WHERE google_sub = ?",
                           (str(sub),)).fetchone()
    return _public(row)


def _google_username(email):
    """Derive a valid, unique username from an email prefix:
    strip characters the username rule rejects, then add a numeric
    suffix until the name is free."""
    base = re.sub(r"[^A-Za-z0-9_.]", "", (email or "").split("@")[0])
    base = base.strip(".")[:24] or "googleuser"
    if len(base) < 3:
        base = (base + "guser")[:24]
    with _db() as conn:
        cand, n = base, 1
        while conn.execute("SELECT 1 FROM users WHERE username = ?",
                           (cand,)).fetchone():
            n += 1
            suffix = str(n)
            cand = base[:24 - len(suffix)] + suffix
    return cand


def create_google_user(sub, email):
    """First Google sign-in for an unseen sub: a new account whose
    password is a random secret nobody knows and whose username is
    auto-derived from the email — BOTH are temporary (build 187):
    the row is stamped needs_setup=1 and the user picks their real
    username + password on the one-time setup screen before they
    can start any job. Credits and ledger start empty, exactly
    like a fresh registration."""
    username = _google_username(email)
    with _LOCK, _db() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, pass_hash, created_at, "
            "google_sub, email, last_login, needs_setup, "
            "referral_code) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            (username, generate_password_hash(secrets.token_urlsafe(48)),
             time.time(), str(sub), email, time.time(),
             _fresh_referral_code(conn)))
        row = conn.execute("SELECT * FROM users WHERE id = ?",
                           (cur.lastrowid,)).fetchone()
    return _public(row)


def link_google(user_id, sub, email):
    """Attach a Google identity to an EXISTING account. Refuses
    (False) when the sub already belongs to a different account —
    the partial unique index is the backstop, never a 500."""
    existing = user_for_google_sub(sub)
    if existing and existing["id"] != int(user_id):
        return False
    try:
        with _LOCK, _db() as conn:
            conn.execute(
                "UPDATE users SET google_sub = ?, email = ? WHERE id = ?",
                (str(sub), email, int(user_id)))
        return True
    except sqlite3.IntegrityError:
        return False


def ensure_admin(user_id):
    """Promote an account to admin. Called ONLY from the Google
    sign-in path after a verified token for the fixed owner email
    (build 180 amendment) — no other caller may exist.

    Build 187: promotion also CLEARS needs_setup. The owner's
    Google-born row is created with the flag like any other; if
    the job-start gate ever looked at it before this promotion,
    the owner could be held at a customer setup screen. Clearing
    it here (plus the gate's admin exemption) makes that
    impossible."""
    with _LOCK, _db() as conn:
        conn.execute("UPDATE users SET role = 'admin', needs_setup = 0 "
                     "WHERE id = ?", (int(user_id),))


def complete_setup(user_id, username, password):
    """One-time username+password setup for a Google-born account
    (build 187). Returns {"user", "already", "error"} — exactly
    one outcome: success (user), a clean no-op when the account
    never needed or already finished setup (already — a rename
    after setup is NOT possible through here), or a Burmese
    validation error the user can fix and retry.

    Username rules are registration's: same regex, unique
    case-insensitively. Keeping the account's OWN auto-derived
    name is allowed — the uniqueness check excludes this row, and
    picking a new name simply replaces the temporary one in the
    same UPDATE, releasing it. The password hash is written
    directly, NOT via set_password: setup must not revoke the
    session the user is completing it from."""
    username = (username or "").strip()
    if not _USERNAME_RE.match(username):
        return {"user": None, "already": False,
                "error": "username ၃–၂၄ လုံး, အင်္ဂလိပ်/ဂဏန်း/./_ ပဲ ရပါတယ်"}
    if not password or len(password) < 6:
        return {"user": None, "already": False,
                "error": "password အနည်းဆုံး ၆ လုံး ထားပါ"}
    with _LOCK, _db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?",
                           (int(user_id),)).fetchone()
        if row is None:
            return {"user": None, "already": False,
                    "error": "account မတွေ့ပါ"}
        if not row["needs_setup"]:
            return {"user": _public(row), "already": True,
                    "error": None}
        taken = conn.execute(
            "SELECT 1 FROM users WHERE username = ? AND id != ?",
            (username, int(user_id))).fetchone()
        if taken:
            return {"user": None, "already": False,
                    "error": "ဒီ username ယူပြီးသား ဖြစ်နေပါတယ်"}
        cur = conn.execute(
            "UPDATE users SET username = ?, pass_hash = ?, "
            "needs_setup = 0 WHERE id = ? AND needs_setup = 1",
            (username, generate_password_hash(password),
             int(user_id)))
        if cur.rowcount != 1:
            # A parallel setup submit won the race; this call is
            # now the no-op second call.
            row = conn.execute("SELECT * FROM users WHERE id = ?",
                               (int(user_id),)).fetchone()
            return {"user": _public(row), "already": True,
                    "error": None}
        row = conn.execute("SELECT * FROM users WHERE id = ?",
                           (int(user_id),)).fetchone()
    return {"user": _public(row), "already": False, "error": None}


def touch_login(user_id):
    with _LOCK, _db() as conn:
        conn.execute("UPDATE users SET last_login = ? WHERE id = ?",
                     (time.time(), int(user_id)))


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


def charge(user_id, amount, ref, reason="job"):
    """Charge `amount` credits for a job; False when insufficient.
    `reason` is the ledger kind: "job" for every video product,
    "download" for the download-only product (build 234) — the
    append-only ledger keeps the products distinguishable."""
    if amount <= 0:
        return True
    return apply_credit(user_id, -int(amount), reason, ref) is not None


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


def create_topup(user_id, credits, proof, package_kyat=None):
    with _LOCK, _db() as conn:
        cur = conn.execute(
            "INSERT INTO topups (user_id, credits, proof, "
            "package_kyat, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (int(user_id), int(credits), proof,
             int(package_kyat) if package_kyat else None,
             time.time()))
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
            "SELECT id, credits, package_kyat, status, "
            "created_at, decided_at "
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
            "SELECT t.credits, t.package_kyat, t.user_id, "
            "u.tg_chat_id FROM topups t "
            "JOIN users u ON u.id = t.user_id WHERE t.id = ?",
            (int(topup_id),)).fetchone()
    if ok:
        # Build 231: a package approval's ledger entry names
        # the package and its bonus, so the one auditable entry
        # shows base + bonus at a glance.
        ref = f"topup:{topup_id}"
        if row["package_kyat"]:
            pkg = int(row["package_kyat"])
            bonus = int(row["credits"]) - pkg // 5
            ref = f"topup:{topup_id}:pkg{pkg}+bonus{bonus}"
        apply_credit(row["user_id"], row["credits"], "topup",
                     ref)
    return {"user_id": row["user_id"], "credits": row["credits"],
            "tg_chat_id": row["tg_chat_id"]}


# ---------------- referrals (build 221) ----------------

def user_for_referral_code(code):
    """Resolve a referral code to its owner (public dict) —
    case-insensitive, surrounding space ignored. None when the
    code is empty or unknown."""
    code = (code or "").strip().upper()
    if not code:
        return None
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE referral_code = ? "
            "COLLATE NOCASE", (code,)).fetchone()
    return _public(row)


def set_referred_by(user_id, inviter_id):
    """Set-once: stamp the inviter on an account that has none
    yet (the profile path — Google-born accounts arrive without
    one). The WHERE guard makes a second set, or a race, a clean
    False; an account can never refer itself."""
    if int(user_id) == int(inviter_id):
        return False
    with _LOCK, _db() as conn:
        cur = conn.execute(
            "UPDATE users SET referred_by = ? WHERE id = ? "
            "AND referred_by IS NULL",
            (int(inviter_id), int(user_id)))
    return cur.rowcount == 1


def referral_stats(user_id):
    """The profile referral block: own code, who invited this
    account (if anyone), how many accounts it has referred, and
    total credits earned from referral rewards (the ledger is
    the source of truth, so the figure can never drift from the
    balance history)."""
    with _db() as conn:
        row = conn.execute(
            "SELECT referral_code, referred_by FROM users "
            "WHERE id = ?", (int(user_id),)).fetchone()
        if row is None:
            return None
        inviter_name = None
        if row["referred_by"]:
            irow = conn.execute(
                "SELECT username FROM users WHERE id = ?",
                (row["referred_by"],)).fetchone()
            inviter_name = irow["username"] if irow else None
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE referred_by = ?",
            (int(user_id),)).fetchone()["n"]
        earned = conn.execute(
            "SELECT COALESCE(SUM(delta), 0) AS s FROM ledger "
            "WHERE user_id = ? AND reason = 'referral'",
            (int(user_id),)).fetchone()["s"]
    return {"code": row["referral_code"],
            "referred_by": row["referred_by"],
            "referred_by_name": inviter_name,
            "count": int(count), "earned": int(earned)}


def pay_referral_reward(referred_user_id, amount):
    """The inviter's one-time reward for a referred account's
    first charged job — EXACTLY ONCE per referred account: the
    referral_rewarded flag flips inside the same locked
    transaction that reads referred_by, so a retry, a re-render
    or a restart can never pay twice. The credit itself goes
    through apply_credit like every other money move (ledger
    reason 'referral', ref 'referral:<referred user id>').
    Returns the amount paid (0 when nothing was due)."""
    amount = int(amount or 0)
    if amount <= 0:
        return 0
    with _LOCK, _db() as conn:
        row = conn.execute(
            "SELECT referred_by, referral_rewarded FROM users "
            "WHERE id = ?", (int(referred_user_id),)).fetchone()
        if row is None or not row["referred_by"] \
                or row["referral_rewarded"]:
            return 0
        cur = conn.execute(
            "UPDATE users SET referral_rewarded = 1 WHERE id = ? "
            "AND COALESCE(referral_rewarded, 0) = 0",
            (int(referred_user_id),))
        if cur.rowcount != 1:
            return 0
        inviter = row["referred_by"]
    apply_credit(inviter, amount, "referral",
                 f"referral:{int(referred_user_id)}")
    return amount


# ---------------- voice clones (build 212) ----------------
# Self-serve Voice Clone: an account holder clones their OWN
# voice at ElevenLabs (IVC, under the owner's API key); the row
# here is the ONLY record of who owns which voice_id. A clone
# belongs to exactly one account: /api/voices' "mine" reads
# these rows, and job creation resolves an "el:<voice_id>" pick
# against them — a clone is usable by its owner alone, whatever
# the registry gate says about catalogue voices.

def add_clone(user_id, voice_id, name):
    """Record a freshly created clone. Returns the row dict.
    voice_id is UNIQUE — a duplicate raises IntegrityError and
    the caller cleans up the orphan ElevenLabs voice."""
    with _LOCK, _db() as conn:
        cur = conn.execute(
            "INSERT INTO clones (user_id, voice_id, name, "
            "created_at) VALUES (?, ?, ?, ?)",
            (int(user_id), str(voice_id), str(name), time.time()))
        row = conn.execute("SELECT * FROM clones WHERE id = ?",
                           (cur.lastrowid,)).fetchone()
    return dict(row)


def clones_for(user_id):
    """The account's clones, in creation order."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM clones WHERE user_id = ? ORDER BY id",
            (int(user_id),)).fetchall()
    return [dict(r) for r in rows]


def clone_by_voice(voice_id):
    """The clone row for a voice_id, whoever owns it — callers
    MUST compare user_id themselves before granting anything."""
    with _db() as conn:
        row = conn.execute("SELECT * FROM clones WHERE voice_id = ?",
                           (str(voice_id),)).fetchone()
    return dict(row) if row else None


def clone_count(user_id):
    with _db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM clones WHERE user_id = ?",
            (int(user_id),)).fetchone()
    return int(row["n"])


def delete_clone(user_id, voice_id):
    """Delete ONLY the caller's own row. True when one went."""
    with _LOCK, _db() as conn:
        cur = conn.execute(
            "DELETE FROM clones WHERE user_id = ? AND voice_id = ?",
            (int(user_id), str(voice_id)))
    return cur.rowcount == 1


# ---------------- voice favourites (build 229) ----------------
# A signed-in user can star any voice in the picker (built-in
# styles, ElevenLabs registry voices, their own clones,
# Gemini voices). The rows are keyed by user_id and read ONLY
# for that user — one account's stars are never visible to
# another. voice_id is the picker's own id vocabulary (a
# style key, "el:<voice_id>", "gm:<Name>").

def fav_voices(user_id):
    """The account's starred voice ids, in starring order."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT voice_id FROM voice_favs WHERE user_id = ? "
            "ORDER BY created_at, voice_id",
            (int(user_id),)).fetchall()
    return [r["voice_id"] for r in rows]


def add_fav(user_id, voice_id):
    with _LOCK, _db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO voice_favs "
            "(user_id, voice_id, created_at) VALUES (?, ?, ?)",
            (int(user_id), str(voice_id), time.time()))


def remove_fav(user_id, voice_id):
    with _LOCK, _db() as conn:
        conn.execute(
            "DELETE FROM voice_favs WHERE user_id = ? "
            "AND voice_id = ?",
            (int(user_id), str(voice_id)))


# ---------------- cover series (build 223) ----------------
# The cover maker remembers, per account, the series names the
# creator has made covers for and the highest part number used,
# so the next part of a series only needs the number bumped.
# Rows are keyed by the case-insensitive name (name_key); the
# display name keeps the casing first typed.

def _series_key(name):
    return (name or "").strip().lower()


def cover_series_list(user_id):
    """The account's cover series, most recently used first:
    [{"name": display name, "last_part": highest part made}]."""
    with _db() as conn:
        rows = conn.execute(
            "SELECT name, last_part FROM cover_series "
            "WHERE user_id = ? ORDER BY updated_at DESC",
            (int(user_id),)).fetchall()
    return [{"name": r["name"], "last_part": int(r["last_part"])}
            for r in rows]


def cover_series_upsert(user_id, name, part=None):
    """Record a cover made for `name`. last_part only ever
    moves UP (MAX), so re-making an earlier part by mistake
    never lowers the remembered number; `part=None` (or 0)
    just registers/touches the series. Returns the stored row
    {"name", "last_part"} — or None for an empty name."""
    name = (name or "").strip()
    if not name:
        return None
    try:
        part = int(part or 0)
    except (TypeError, ValueError):
        part = 0
    if part < 0:
        part = 0
    with _LOCK, _db() as conn:
        conn.execute(
            "INSERT INTO cover_series (user_id, name, name_key, "
            "last_part, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, name_key) DO UPDATE SET "
            "last_part = MAX(last_part, excluded.last_part), "
            "updated_at = excluded.updated_at",
            (int(user_id), name, _series_key(name), part,
             time.time()))
        row = conn.execute(
            "SELECT name, last_part FROM cover_series "
            "WHERE user_id = ? AND name_key = ?",
            (int(user_id), _series_key(name))).fetchone()
    return {"name": row["name"],
            "last_part": int(row["last_part"])}


# ---------------- API tokens (build 224) ----------------
# The reseller API authenticates with a per-account Bearer
# token, fully separate from web sessions: a token is NOT a
# session (it never touches the sessions table) and a session
# cookie never authenticates an API call. Each account has at
# most ONE live token; generating a new one replaces the old
# (the old token dies in the same UPDATE), revoking clears it.
# Only the SHA-256 hash is stored — the plaintext is returned
# to the caller exactly once, at generation, and the profile
# can afterwards only report THAT a token exists.

_API_TOKEN_PREFIX = "vt_"


def _api_token_hash(token):
    return hashlib.sha256(
        (token or "").encode("utf-8")).hexdigest()


def set_api_token(user_id):
    """Generate (or regenerate) the account's API token and
    return the PLAINTEXT — the one and only time it exists
    outside the owner's hands. Regeneration invalidates the
    previous token immediately (same row, new hash)."""
    token = _API_TOKEN_PREFIX + secrets.token_hex(20)
    with _LOCK, _db() as conn:
        conn.execute(
            "UPDATE users SET api_token_hash = ? WHERE id = ?",
            (_api_token_hash(token), int(user_id)))
    return token


def revoke_api_token(user_id):
    """Clear the account's API token (True when one existed)."""
    with _LOCK, _db() as conn:
        cur = conn.execute(
            "UPDATE users SET api_token_hash = NULL "
            "WHERE id = ? AND api_token_hash IS NOT NULL",
            (int(user_id),))
    return cur.rowcount == 1


def has_api_token(user_id):
    """Whether the account currently has a live API token —
    all the profile is ever allowed to know after generation."""
    with _db() as conn:
        row = conn.execute(
            "SELECT api_token_hash FROM users WHERE id = ?",
            (int(user_id),)).fetchone()
    return bool(row and row["api_token_hash"])


def user_for_api_token(token):
    """Resolve a presented Bearer token to its account (public
    dict) — the API's ONLY auth path. The presented value is
    hashed and matched against stored hashes; a disabled
    account's token is refused, exactly like its sessions."""
    token = (token or "").strip()
    if not token:
        return None
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE api_token_hash = ?",
            (_api_token_hash(token),)).fetchone()
    if row is None or row["disabled"]:
        return None
    return _public(row)
