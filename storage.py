import os
import hashlib
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import config

SCHEDULE_CACHE_TTL = 600  # 10 минут
STARTING_COINS = 3

_FERNET = None
_ENCRYPTION_MISSING_WARNED = False


class TokenEncryptionKeyMissing(RuntimeError):
    pass


def _get_fernet():
    global _FERNET, _ENCRYPTION_MISSING_WARNED
    if _FERNET is not None:
        return _FERNET
    if not config.TOKEN_ENCRYPTION_KEY:
        if not _ENCRYPTION_MISSING_WARNED:
            _ENCRYPTION_MISSING_WARNED = True
            import logging
            logging.getLogger("storage").warning(
                "Ключ шифрования не найден — токены будут храниться в открытом виде.\n"
                "Создайте encryption_key.txt или задайте TOKEN_ENCRYPTION_KEY."
            )
        return None
    try:
        from cryptography.fernet import Fernet
        _FERNET = Fernet(config.TOKEN_ENCRYPTION_KEY.encode())
    except Exception as e:
        raise TokenEncryptionKeyMissing(f"TOKEN_ENCRYPTION_KEY невалиден: {e}")
    return _FERNET


def ensure_encryption_key():
    _get_fernet()


def _encrypt(plaintext: str) -> str:
    f = _get_fernet()
    if f is None:
        return plaintext
    return f.encrypt(plaintext.encode()).decode()


def _decrypt(stored: str) -> str:
    f = _get_fernet()
    if f is None:
        return stored
    try:
        return f.decrypt(stored.encode()).decode()
    except Exception:
        return stored


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id      TEXT PRIMARY KEY,
                sirius_token TEXT NOT NULL,
                token_saved_at INTEGER NOT NULL,
                created_at   INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                session_id   TEXT PRIMARY KEY,
                user_id      TEXT NOT NULL,
                created_at   INTEGER NOT NULL,
                last_active  INTEGER NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );

            CREATE TABLE IF NOT EXISTS watchlist (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      TEXT NOT NULL,
                event_id     TEXT NOT NULL,
                event_name   TEXT NOT NULL,
                priority     INTEGER NOT NULL DEFAULT 100,
                status       TEXT NOT NULL DEFAULT 'watching',
                added_at     INTEGER NOT NULL,
                updated_at   INTEGER NOT NULL,
                event_start  TEXT DEFAULT '',
                snipe_priority TEXT NOT NULL DEFAULT 'high',
                coin_cost    INTEGER NOT NULL DEFAULT 1,
                UNIQUE(user_id, event_id)
            );

            CREATE TABLE IF NOT EXISTS events_cache (
                user_id      TEXT PRIMARY KEY,
                data_json    TEXT NOT NULL,
                cached_at    INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS snipe_attempts (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      TEXT NOT NULL,
                event_id     TEXT NOT NULL,
                event_name   TEXT NOT NULL DEFAULT '',
                phase        TEXT NOT NULL,
                status_code  INTEGER,
                success      INTEGER NOT NULL DEFAULT 0,
                reserved     INTEGER NOT NULL DEFAULT 0,
                message      TEXT NOT NULL DEFAULT '',
                latency_ms   INTEGER NOT NULL DEFAULT 0,
                created_at   INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_snipe_attempts_user_event
                ON snipe_attempts(user_id, event_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS schedule_cache (
                user_id      TEXT NOT NULL,
                date         TEXT NOT NULL,
                data_json    TEXT NOT NULL,
                cached_at    INTEGER NOT NULL,
                UNIQUE(user_id, date)
            );

            CREATE TABLE IF NOT EXISTS schedule_reminders (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      TEXT NOT NULL,
                event_id     TEXT NOT NULL,
                event_name   TEXT NOT NULL,
                event_start  TEXT NOT NULL,
                minutes_before INTEGER NOT NULL,
                created_at   INTEGER NOT NULL,
                UNIQUE(user_id, event_id)
            );

            CREATE TABLE IF NOT EXISTS event_snapshots (
                user_id      TEXT PRIMARY KEY,
                data_json    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS notifications (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      TEXT NOT NULL,
                text         TEXT NOT NULL,
                type         TEXT NOT NULL DEFAULT 'info',
                created_at   INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS custom_events (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      TEXT NOT NULL,
                event_name   TEXT NOT NULL,
                date_iso     TEXT NOT NULL,
                start_time   TEXT NOT NULL DEFAULT '',
                end_time     TEXT NOT NULL DEFAULT '',
                description  TEXT NOT NULL DEFAULT '',
                location     TEXT NOT NULL DEFAULT '',
                repeat_daily INTEGER NOT NULL DEFAULT 0,
                created_at   INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sirius_coins (
                uid          TEXT PRIMARY KEY,
                coins        INTEGER NOT NULL DEFAULT 0,
                created_at   INTEGER NOT NULL,
                updated_at   INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS admins (
                uid          TEXT PRIMARY KEY,
                created_at   INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS account_trust (
                uid          TEXT PRIMARY KEY,
                trust_level  INTEGER NOT NULL DEFAULT 2,
                updated_at   INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS known_uids (
                uid          TEXT PRIMARY KEY,
                user_id      TEXT NOT NULL,
                full_name    TEXT NOT NULL DEFAULT '',
                updated_at   INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS login_codes (
                code_hash    TEXT PRIMARY KEY,
                user_id      TEXT NOT NULL,
                created_at   INTEGER NOT NULL,
                expires_at   INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS push_subscriptions (
                endpoint          TEXT PRIMARY KEY,
                user_id           TEXT NOT NULL,
                subscription_json TEXT NOT NULL,
                created_at        INTEGER NOT NULL,
                updated_at        INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS feedback_messages (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      TEXT NOT NULL DEFAULT '',
                message      TEXT NOT NULL,
                answer       TEXT NOT NULL DEFAULT '',
                answered_at  INTEGER NOT NULL DEFAULT 0,
                answered_by  TEXT NOT NULL DEFAULT '',
                user_hidden  INTEGER NOT NULL DEFAULT 0,
                admin_hidden INTEGER NOT NULL DEFAULT 0,
                is_read      INTEGER NOT NULL DEFAULT 0,
                created_at   INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS app_meta (
                key          TEXT PRIMARY KEY,
                value        TEXT NOT NULL
            );
        """)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(watchlist)").fetchall()]
        if "event_start" not in cols:
            conn.execute("ALTER TABLE watchlist ADD COLUMN event_start TEXT DEFAULT ''")
        if "snipe_priority" not in cols:
            conn.execute("ALTER TABLE watchlist ADD COLUMN snipe_priority TEXT NOT NULL DEFAULT 'high'")
        if "coin_cost" not in cols:
            conn.execute("ALTER TABLE watchlist ADD COLUMN coin_cost INTEGER NOT NULL DEFAULT 1")

        user_cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "login_email" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN login_email TEXT DEFAULT ''")
        if "login_password" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN login_password TEXT DEFAULT ''")
        if "uid" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN uid TEXT DEFAULT ''")
        if "login_type" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN login_type TEXT DEFAULT ''")
        if "last_token_refresh" not in user_cols:
            conn.execute("ALTER TABLE users ADD COLUMN last_token_refresh INTEGER DEFAULT 0")

        coin_cols = [r["name"] for r in conn.execute("PRAGMA table_info(sirius_coins)").fetchall()]
        if "reserved_coins" not in coin_cols:
            conn.execute("ALTER TABLE sirius_coins ADD COLUMN reserved_coins INTEGER NOT NULL DEFAULT 0")
        conn.execute(
            """UPDATE sirius_coins
               SET reserved_coins = MAX(0, reserved_coins - (
                   SELECT COALESCE(SUM(w.coin_cost), 0)
                   FROM watchlist w
                   LEFT JOIN users u ON u.user_id = w.user_id
                   WHERE w.status='watching'
                     AND w.snipe_priority='low'
                     AND w.coin_cost > 0
                     AND (u.uid = sirius_coins.uid OR w.user_id = sirius_coins.uid)
               ))"""
        )
        conn.execute("UPDATE watchlist SET coin_cost=0 WHERE snipe_priority='low' AND coin_cost!=0")

        feedback_cols = [r["name"] for r in conn.execute("PRAGMA table_info(feedback_messages)").fetchall()]
        if "answer" not in feedback_cols:
            conn.execute("ALTER TABLE feedback_messages ADD COLUMN answer TEXT NOT NULL DEFAULT ''")
        if "answered_at" not in feedback_cols:
            conn.execute("ALTER TABLE feedback_messages ADD COLUMN answered_at INTEGER NOT NULL DEFAULT 0")
        if "answered_by" not in feedback_cols:
            conn.execute("ALTER TABLE feedback_messages ADD COLUMN answered_by TEXT NOT NULL DEFAULT ''")
        if "user_hidden" not in feedback_cols:
            conn.execute("ALTER TABLE feedback_messages ADD COLUMN user_hidden INTEGER NOT NULL DEFAULT 0")
        if "admin_hidden" not in feedback_cols:
            conn.execute("ALTER TABLE feedback_messages ADD COLUMN admin_hidden INTEGER NOT NULL DEFAULT 0")

        security_fix = conn.execute(
            "SELECT value FROM app_meta WHERE key='sessions_invalidated_after_token_validation'"
        ).fetchone()
        if not security_fix:
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM login_codes")
            conn.execute(
                "INSERT INTO app_meta (key, value) VALUES (?, ?)",
                ("sessions_invalidated_after_token_validation", str(int(time.time()))),
            )


@contextmanager
def get_conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------- sessions ----------

def create_session() -> tuple[str, str]:
    session_id = uuid.uuid4().hex
    user_id = uuid.uuid4().hex
    now = int(time.time())
    with get_conn() as conn:
        conn.execute("INSERT INTO sessions (session_id, user_id, created_at, last_active) VALUES (?, ?, ?, ?)",
                     (session_id, user_id, now, now))
    return session_id, user_id


def create_session_for_user(user_id: str) -> str:
    session_id = uuid.uuid4().hex
    now = int(time.time())
    with get_conn() as conn:
        conn.execute("INSERT INTO sessions (session_id, user_id, created_at, last_active) VALUES (?, ?, ?, ?)",
                     (session_id, user_id, now, now))
    return session_id


def get_user_by_session(session_id: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT user_id FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if row:
            conn.execute("UPDATE sessions SET last_active=? WHERE session_id=?", (int(time.time()), session_id))
            return row["user_id"]
    return None


def update_session_user_id(session_id: str, new_user_id: str):
    with get_conn() as conn:
        conn.execute("UPDATE sessions SET user_id=? WHERE session_id=?", (new_user_id, session_id))


def _normalize_login_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def _login_code_hash(code: str) -> str:
    return hashlib.sha256(_normalize_login_code(code).encode()).hexdigest()


def create_login_code(user_id: str, ttl_seconds: int = 600) -> tuple[str, int]:
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    now = int(time.time())
    expires_at = now + ttl_seconds
    with get_conn() as conn:
        conn.execute("DELETE FROM login_codes WHERE user_id=? OR expires_at < ?", (user_id, now))
        for _ in range(10):
            code = "".join(secrets.choice(alphabet) for _ in range(8))
            try:
                conn.execute(
                    "INSERT INTO login_codes (code_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                    (_login_code_hash(code), user_id, now, expires_at),
                )
                return code, expires_at
            except sqlite3.IntegrityError:
                continue
    raise RuntimeError("Не удалось создать код входа")


def consume_login_code(code: str) -> str | None:
    normalized = _normalize_login_code(code)
    if len(normalized) < 6:
        return None
    code_hash = _login_code_hash(normalized)
    now = int(time.time())
    with get_conn() as conn:
        conn.execute("DELETE FROM login_codes WHERE expires_at < ?", (now,))
        row = conn.execute(
            "SELECT user_id FROM login_codes WHERE code_hash=? AND expires_at>=?",
            (code_hash, now),
        ).fetchone()
        conn.execute("DELETE FROM login_codes WHERE code_hash=?", (code_hash,))
        return row["user_id"] if row else None


# ---------- users ----------

def save_token(user_id: str, token: str):
    with get_conn() as conn:
        encrypted = _encrypt(token)
        conn.execute(
            """INSERT INTO users (user_id, sirius_token, token_saved_at, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 sirius_token=excluded.sirius_token,
                 token_saved_at=excluded.token_saved_at""",
            (user_id, encrypted, int(time.time()), int(time.time())),
        )


def save_login_credentials(user_id: str, email: str, password: str):
    with get_conn() as conn:
        conn.execute(
            """UPDATE users SET login_email=?, login_password=?, login_type='password'
               WHERE user_id=?""",
            (_encrypt(email), _encrypt(password), user_id),
        )


def set_login_type(user_id: str, login_type: str):
    with get_conn() as conn:
        conn.execute("UPDATE users SET login_type=? WHERE user_id=?", (login_type, user_id))


def get_login_type(user_id: str) -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT login_type, login_email FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            return ""
        lt = row["login_type"]
        if not lt:
            # Бэкфилл: если есть сохранённый пароль — значит вход по паролю
            if row["login_email"]:
                return "password"
            return "token"
        return lt


def get_last_token_refresh(user_id: str) -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT last_token_refresh FROM users WHERE user_id=?", (user_id,)).fetchone()
        return row["last_token_refresh"] if row else 0


def set_last_token_refresh(user_id: str):
    with get_conn() as conn:
        conn.execute("UPDATE users SET last_token_refresh=? WHERE user_id=?", (int(time.time()), user_id))


def get_user_by_uid(uid: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT user_id FROM users WHERE uid=? OR user_id=?",
            (uid, uid),
        ).fetchone()
        return row["user_id"] if row else None


def get_user_uid(user_id: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT user_id, uid FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            return None
        return row["uid"] or row["user_id"]


def set_user_uid(user_id: str, uid: str):
    """Делает Sirius UID основным идентификатором пользователя."""
    with get_conn() as conn:
        if user_id == uid:
            conn.execute("UPDATE users SET uid=? WHERE user_id=?", (uid, uid))
            return uid
        # Переносим данные во всех таблицах на UID
        tables_with_user_id = [
            "watchlist", "custom_events", "notifications",
            "schedule_cache", "schedule_reminders", "event_snapshots",
            "sessions",
        ]
        for table in tables_with_user_id:
            try:
                conn.execute(
                    f"UPDATE OR IGNORE {table} SET user_id=? WHERE user_id=?",
                    (uid, user_id),
                )
                conn.execute(
                    f"DELETE FROM {table} WHERE user_id=?",
                    (user_id,),
                )
            except Exception:
                pass
        # Переносим запись users (или создаём новую)
        old = conn.execute(
            "SELECT sirius_token, token_saved_at, created_at, login_email, login_password FROM users WHERE user_id=?",
            (user_id,),
        ).fetchone()
        if old:
            conn.execute(
                """INSERT OR REPLACE INTO users (user_id, uid, sirius_token, token_saved_at, created_at, login_email, login_password)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (uid, uid, old["sirius_token"], old["token_saved_at"], old["created_at"],
                 old["login_email"], old["login_password"]),
            )
            conn.execute("DELETE FROM users WHERE user_id=? AND user_id!=?", (user_id, uid))
        else:
            conn.execute(
                "INSERT OR IGNORE INTO users (user_id, uid, sirius_token, token_saved_at, created_at) VALUES (?, ?, '', ?, ?)",
                (uid, uid, int(time.time()), int(time.time())),
            )
        return uid


def migrate_user_data(from_user_id: str, to_user_id: str):
    """Move all data from one user_id to another. Skips conflicts (keeps existing data)."""
    tables = [
        "watchlist", "custom_events", "notifications",
        "schedule_cache", "schedule_reminders", "event_snapshots",
    ]
    with get_conn() as conn:
        for table in tables:
            try:
                conn.execute(
                    f"UPDATE OR IGNORE {table} SET user_id=? WHERE user_id=?",
                    (to_user_id, from_user_id),
                )
                conn.execute(
                    f"DELETE FROM {table} WHERE user_id=?",
                    (from_user_id,),
                )
            except Exception:
                pass


def get_login_credentials(user_id: str) -> tuple[str, str] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT login_email, login_password FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row and row["login_email"]:
            try:
                return _decrypt(row["login_email"]), _decrypt(row["login_password"])
            except Exception:
                return None
    return None


def get_token(user_id: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT sirius_token FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            return None
        stored = row["sirius_token"]
        if not stored:
            return None
        token = _decrypt(stored)
        if token == stored:
            conn.execute("UPDATE users SET sirius_token=? WHERE user_id=?", (_encrypt(token), user_id))
        return token


def delete_token(user_id: str):
    with get_conn() as conn:
        conn.execute("UPDATE users SET sirius_token='' WHERE user_id=?", (user_id,))


# ---------- watchlist ----------

def snipe_priority_cost(snipe_priority: str) -> int:
    if snipe_priority == "low":
        return 0
    return 2 if snipe_priority == "high" else 1


def snipe_priority_multiplier(snipe_priority: str) -> float:
    if snipe_priority == "medium":
        return 3.0
    if snipe_priority == "low":
        return 10.0
    return 1.0


def add_watch(user_id: str, event_id: str, event_name: str, priority: int = 100, event_start: str = "", snipe_priority: str = "high"):
    now = int(time.time())
    if snipe_priority not in ("high", "medium", "low"):
        snipe_priority = "high"
    coin_cost = snipe_priority_cost(snipe_priority)
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO watchlist (user_id, event_id, event_name, priority, status, added_at, updated_at, event_start, snipe_priority, coin_cost)
               VALUES (?, ?, ?, ?, 'watching', ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, event_id) DO UPDATE SET
                 status='watching',
                 updated_at=excluded.updated_at,
                 event_start=excluded.event_start,
                 snipe_priority=excluded.snipe_priority,
                 coin_cost=excluded.coin_cost""",
            (user_id, event_id, event_name, priority, now, now, event_start, snipe_priority, coin_cost),
        )


def remove_watch(user_id: str, event_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM watchlist WHERE user_id=? AND event_id=?", (user_id, event_id))


def update_watch_priority(user_id: str, event_id: str, snipe_priority: str, coin_cost: int):
    now = int(time.time())
    with get_conn() as conn:
        conn.execute(
            "UPDATE watchlist SET snipe_priority=?, coin_cost=?, updated_at=? WHERE user_id=? AND event_id=?",
            (snipe_priority, coin_cost, now, user_id, event_id),
        )


def set_watch_status(user_id: str, event_id: str, status: str, event_start: str | None = None):
    now = int(time.time())
    with get_conn() as conn:
        if event_start is not None:
            conn.execute("UPDATE watchlist SET status=?, updated_at=?, event_start=? WHERE user_id=? AND event_id=?",
                         (status, now, event_start, user_id, event_id))
        else:
            conn.execute("UPDATE watchlist SET status=?, updated_at=? WHERE user_id=? AND event_id=?",
                         (status, now, user_id, event_id))


def get_watchlist(user_id: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM watchlist WHERE user_id=? AND status='watching' ORDER BY priority",
            (user_id,),
        ).fetchall()


def get_watch(user_id: str, event_id: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM watchlist WHERE user_id=? AND event_id=?",
            (user_id, event_id),
        ).fetchone()


def get_all_watchlist(user_id: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM watchlist WHERE user_id=? ORDER BY priority",
            (user_id,),
        ).fetchall()


def get_all_active_watches() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT DISTINCT user_id, event_id, event_name, snipe_priority, coin_cost FROM watchlist WHERE status='watching'"
        ).fetchall()


def get_all_users_with_tokens() -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT user_id FROM users WHERE sirius_token IS NOT NULL AND sirius_token != ''"
        ).fetchall()
        return [r["user_id"] for r in rows]

def get_all_reminders() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM schedule_reminders").fetchall()


# ---------- persistent events cache ----------

def set_events_cache(user_id: str, data_json: str):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO events_cache (user_id, data_json, cached_at)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 data_json=excluded.data_json,
                 cached_at=excluded.cached_at""",
            (user_id, data_json, int(time.time())),
        )


def get_events_cache(user_id: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT data_json, cached_at FROM events_cache WHERE user_id=?",
            (user_id,),
        ).fetchone()


# ---------- snipe attempt log ----------

def add_snipe_attempt(
    user_id: str,
    event_id: str,
    event_name: str,
    phase: str,
    status_code: int | None = None,
    success: bool = False,
    reserved: bool = False,
    message: str = "",
    latency_ms: int = 0,
):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO snipe_attempts
               (user_id, event_id, event_name, phase, status_code, success, reserved, message, latency_ms, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                event_id,
                event_name,
                phase,
                status_code,
                1 if success else 0,
                1 if reserved else 0,
                (message or "")[:500],
                int(latency_ms or 0),
                int(time.time()),
            ),
        )


def get_snipe_attempts(user_id: str, event_id: str, limit: int = 60) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT * FROM snipe_attempts
               WHERE user_id=? AND event_id=?
               ORDER BY created_at DESC, id DESC
               LIMIT ?""",
            (user_id, event_id, limit),
        ).fetchall()


def cleanup_snipe_attempts(max_age_days: int = 14):
    cutoff = int(time.time()) - max_age_days * 24 * 3600
    with get_conn() as conn:
        conn.execute("DELETE FROM snipe_attempts WHERE created_at < ?", (cutoff,))


# ---------- custom events ----------

def add_custom_event(user_id: str, event_name: str, date_iso: str, start_time: str = "", end_time: str = "", description: str = "", location: str = "", repeat_daily: bool = False) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO custom_events (user_id, event_name, date_iso, start_time, end_time, description, location, repeat_daily, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, event_name, date_iso, start_time, end_time, description, location, 1 if repeat_daily else 0, int(time.time())),
        )
        return cur.lastrowid

def get_custom_events(user_id: str, date_iso: str = "") -> list[sqlite3.Row]:
    with get_conn() as conn:
        if date_iso:
            return conn.execute(
                "SELECT * FROM custom_events WHERE user_id=? AND date_iso=? ORDER BY start_time",
                (user_id, date_iso),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM custom_events WHERE user_id=? ORDER BY date_iso, start_time",
            (user_id,),
        ).fetchall()

def get_all_custom_events() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM custom_events").fetchall()

def get_custom_events_for_date(user_id: str, date_iso: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM custom_events WHERE user_id=? AND (date_iso=? OR (repeat_daily=1 AND date_iso<=?)) ORDER BY start_time",
            (user_id, date_iso, date_iso),
        ).fetchall()

def remove_custom_event(user_id: str, event_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM custom_events WHERE user_id=? AND id=?", (user_id, event_id))

def is_daily_custom_event(event_id: str) -> bool:
    """Проверяет, является ли event_id (формата 'custom_<id>')
    ежедневным повторяющимся событием."""
    if not event_id.startswith("custom_"):
        return False
    try:
        eid = int(event_id.split("_", 1)[1])
    except (ValueError, IndexError):
        return False
    with get_conn() as conn:
        row = conn.execute("SELECT repeat_daily FROM custom_events WHERE id=?", (eid,)).fetchone()
        return bool(row and row["repeat_daily"])


# ---------- event snapshots ----------

def get_event_snapshot(user_id: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT data_json FROM event_snapshots WHERE user_id=?", (user_id,)).fetchone()
        return row["data_json"] if row else None


def set_event_snapshot(user_id: str, data_json: str):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO event_snapshots (user_id, data_json)
               VALUES (?, ?)
               ON CONFLICT(user_id) DO UPDATE SET data_json=excluded.data_json""",
            (user_id, data_json),
        )


# ---------- cleanup ----------

# ---------- notifications ----------

def add_notification(user_id: str, text: str, type: str = "info"):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO notifications (user_id, text, type, created_at) VALUES (?, ?, ?, ?)",
            (user_id, text, type, int(time.time())),
        )

def get_notifications(user_id: str, limit: int = 100) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()

def clear_notifications(user_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM notifications WHERE user_id=?", (user_id,))


def delete_notification(user_id: str, notif_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM notifications WHERE user_id=? AND id=?", (user_id, notif_id))


# ---------- feedback ----------

def add_feedback(user_id: str | None, message: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO feedback_messages (user_id, message, created_at) VALUES (?, ?, ?)",
            (user_id or "", message, int(time.time())),
        )


def get_feedback_messages(limit: int = 200) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT f.id, f.user_id, f.message, f.answer, f.answered_at, f.answered_by, f.is_read, f.created_at,
                      COALESCE(k.full_name, '') AS full_name
               FROM feedback_messages f
               LEFT JOIN known_uids k ON k.user_id = f.user_id
               WHERE f.admin_hidden=0
               ORDER BY f.created_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()


def get_user_feedback_messages(user_id: str, limit: int = 50) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT id, message, answer, answered_at, answered_by, is_read, created_at
               FROM feedback_messages
               WHERE user_id=? AND user_hidden=0
               ORDER BY created_at DESC
               LIMIT ?""",
            (user_id, limit),
        ).fetchall()


def mark_feedback_read(feedback_id: int, is_read: bool = True):
    with get_conn() as conn:
        conn.execute(
            "UPDATE feedback_messages SET is_read=? WHERE id=?",
            (1 if is_read else 0, feedback_id),
        )


def answer_feedback(feedback_id: int, answer: str, answered_by: str) -> sqlite3.Row | None:
    now = int(time.time())
    with get_conn() as conn:
        conn.execute(
            "UPDATE feedback_messages SET answer=?, answered_at=?, answered_by=?, is_read=1 WHERE id=?",
            (answer, now, answered_by, feedback_id),
        )
        return conn.execute(
            "SELECT id, user_id, message, answer, answered_at FROM feedback_messages WHERE id=?",
            (feedback_id,),
        ).fetchone()


def delete_feedback(feedback_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM feedback_messages WHERE id=?", (feedback_id,))


def hide_feedback_for_admin(feedback_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE feedback_messages SET admin_hidden=1 WHERE id=?", (feedback_id,))


def hide_feedback_for_user(user_id: str, feedback_id: int):
    with get_conn() as conn:
        conn.execute(
            "UPDATE feedback_messages SET user_hidden=1 WHERE id=? AND user_id=?",
            (feedback_id, user_id),
        )


# ---------- push subscriptions ----------

def save_push_subscription(user_id: str, subscription_json: str, endpoint: str):
    now = int(time.time())
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO push_subscriptions (endpoint, user_id, subscription_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(endpoint) DO UPDATE SET
                 user_id=excluded.user_id,
                 subscription_json=excluded.subscription_json,
                 updated_at=excluded.updated_at""",
            (endpoint, user_id, subscription_json, now, now),
        )


def delete_push_subscription(endpoint: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))


def get_push_subscriptions(user_id: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM push_subscriptions WHERE user_id=?",
            (user_id,),
        ).fetchall()


def cleanup_schedule_cache():
    with get_conn() as conn:
        conn.execute("DELETE FROM schedule_cache WHERE cached_at < ?", (int(time.time()) - SCHEDULE_CACHE_TTL,))


# ---------- schedule cache ----------

def get_schedule_cache(user_id: str, date: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT data_json, cached_at FROM schedule_cache WHERE user_id=? AND date=?",
            (user_id, date),
        ).fetchone()
        if not row:
            return None
        if time.time() - row["cached_at"] > SCHEDULE_CACHE_TTL:
            conn.execute("DELETE FROM schedule_cache WHERE user_id=? AND date=?", (user_id, date))
            return None
        return row["data_json"]


def set_schedule_cache(user_id: str, date: str, data_json: str):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO schedule_cache (user_id, date, data_json, cached_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id, date) DO UPDATE SET
                 data_json=excluded.data_json, cached_at=excluded.cached_at""",
            (user_id, date, data_json, int(time.time())),
        )


# ---------- schedule reminders ----------

def add_reminder(user_id: str, event_id: str, event_name: str, event_start: str, minutes_before: int):
    now = int(time.time())
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO schedule_reminders (user_id, event_id, event_name, event_start, minutes_before, created_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(user_id, event_id) DO UPDATE SET
                 minutes_before=excluded.minutes_before, created_at=excluded.created_at""",
            (user_id, event_id, event_name, event_start, minutes_before, now),
        )


def remove_reminder(user_id: str, event_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM schedule_reminders WHERE user_id=? AND event_id=?", (user_id, event_id))


def get_reminders_for_user(user_id: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM schedule_reminders WHERE user_id=? ORDER BY event_start",
            (user_id,),
        ).fetchall()


# ---------- sirius coins ----------

def ensure_coins(uid: str) -> int:
    """Создаёт запись со стартовыми коинами, если ещё нет. Возвращает доступный баланс."""
    now = int(time.time())
    with get_conn() as conn:
        row = conn.execute("SELECT coins, reserved_coins FROM sirius_coins WHERE uid=?", (uid,)).fetchone()
        if row:
            return row["coins"] - row["reserved_coins"]
        conn.execute(
            "INSERT INTO sirius_coins (uid, coins, reserved_coins, created_at, updated_at) VALUES (?, ?, 0, ?, ?)",
            (uid, STARTING_COINS, now, now),
        )
        return STARTING_COINS


def get_coins(uid: str) -> int:
    """Доступные коины (всего минус в резерве)."""
    with get_conn() as conn:
        row = conn.execute("SELECT coins, reserved_coins FROM sirius_coins WHERE uid=?", (uid,)).fetchone()
        if not row:
            return 0
        return row["coins"] - row["reserved_coins"]


def get_coins_total(uid: str) -> int:
    """Общее количество коинов (включая зарезервированные)."""
    with get_conn() as conn:
        row = conn.execute("SELECT coins FROM sirius_coins WHERE uid=?", (uid,)).fetchone()
        return row["coins"] if row else 0


def get_coins_reserved(uid: str) -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT reserved_coins FROM sirius_coins WHERE uid=?", (uid,)).fetchone()
        return row["reserved_coins"] if row else 0


def reserve_coins(uid: str, amount: int = 1) -> bool:
    """Резервирует коины для снайпа. Возвращает False если доступных нет."""
    now = int(time.time())
    amount = max(0, int(amount))
    if amount == 0:
        return True
    with get_conn() as conn:
        row = conn.execute("SELECT coins, reserved_coins FROM sirius_coins WHERE uid=?", (uid,)).fetchone()
        if not row:
            return False
        available = row["coins"] - row["reserved_coins"]
        if available < amount:
            return False
        conn.execute(
            "UPDATE sirius_coins SET reserved_coins=reserved_coins+?, updated_at=? WHERE uid=?",
            (amount, now, uid),
        )
        return True


def reserve_coin(uid: str) -> bool:
    return reserve_coins(uid, 1)


def release_coins(uid: str, amount: int = 1):
    """Возвращает зарезервированные коины в доступные."""
    now = int(time.time())
    amount = max(0, int(amount))
    if amount == 0:
        return
    with get_conn() as conn:
        conn.execute(
            "UPDATE sirius_coins SET reserved_coins=MAX(0, reserved_coins-?), updated_at=? WHERE uid=?",
            (amount, now, uid),
        )


def release_coin(uid: str):
    release_coins(uid, 1)


def spend_reserved_coins(uid: str, amount: int = 1) -> bool:
    """Списывает зарезервированные коины окончательно."""
    now = int(time.time())
    amount = max(0, int(amount))
    if amount == 0:
        return True
    with get_conn() as conn:
        row = conn.execute("SELECT coins, reserved_coins FROM sirius_coins WHERE uid=?", (uid,)).fetchone()
        if not row or row["reserved_coins"] < amount or row["coins"] < amount:
            return False
        conn.execute(
            "UPDATE sirius_coins SET coins=coins-?, reserved_coins=reserved_coins-?, updated_at=? WHERE uid=?",
            (amount, amount, now, uid),
        )
        return True


def spend_reserved_coin(uid: str) -> bool:
    return spend_reserved_coins(uid, 1)


def add_coins(uid: str, amount: int) -> int:
    """Добавляет amount коинов. Возвращает новый общий баланс."""
    now = int(time.time())
    with get_conn() as conn:
        row = conn.execute("SELECT coins FROM sirius_coins WHERE uid=?", (uid,)).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO sirius_coins (uid, coins, reserved_coins, created_at, updated_at) VALUES (?, ?, 0, ?, ?)",
                (uid, amount, now, now),
            )
            return amount
        new_balance = row["coins"] + amount
        conn.execute(
            "UPDATE sirius_coins SET coins=?, updated_at=? WHERE uid=?",
            (new_balance, now, uid),
        )
        return new_balance


def spend_coin(uid: str) -> bool:
    """Списывает 1 зарезервированный коин. Для совместимости со старым API."""
    return spend_reserved_coin(uid)


# ---------- admins ----------

def is_admin(uid: str) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM admins WHERE uid=?", (uid,)).fetchone()
        return bool(row)


def add_admin(uid: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO admins (uid, created_at) VALUES (?, ?)",
            (uid, int(time.time())),
        )


def remove_admin(uid: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM admins WHERE uid=?", (uid,))


# ---------- known uids ----------

def save_known_uid(uid: str, user_id: str, full_name: str = ""):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO known_uids (uid, user_id, full_name, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(uid) DO UPDATE SET
                 user_id=excluded.user_id,
                 full_name=excluded.full_name,
                 updated_at=excluded.updated_at""",
            (uid, user_id, full_name, int(time.time())),
        )


def get_all_known_uids() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT k.uid, k.full_name, COALESCE(c.coins, 0) as coins, "
            "COALESCE(t.trust_level, 2) as trust_level, "
            "CASE WHEN a.uid IS NULL THEN 0 ELSE 1 END as is_admin, k.updated_at "
            "FROM known_uids k LEFT JOIN sirius_coins c ON k.uid = c.uid "
            "LEFT JOIN account_trust t ON k.uid = t.uid "
            "LEFT JOIN admins a ON k.uid = a.uid "
            "ORDER BY k.updated_at DESC"
        ).fetchall()


def get_trust_level(uid: str) -> int:
    if not uid:
        return 2
    if is_admin(uid):
        return 0
    with get_conn() as conn:
        row = conn.execute("SELECT trust_level FROM account_trust WHERE uid=?", (uid,)).fetchone()
        return int(row["trust_level"]) if row else 2


def set_trust_level(uid: str, trust_level: int):
    if trust_level not in (1, 2, 3):
        raise ValueError("trust_level must be 1, 2 or 3")
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO account_trust (uid, trust_level, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(uid) DO UPDATE SET
                 trust_level=excluded.trust_level,
                 updated_at=excluded.updated_at""",
            (uid, trust_level, int(time.time())),
        )
