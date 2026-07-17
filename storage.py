import os
import hashlib
import json
import math
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

            CREATE TABLE IF NOT EXISTS community_events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_user_id   TEXT NOT NULL,
                owner_uid       TEXT NOT NULL,
                event_name      TEXT NOT NULL,
                date_iso        TEXT NOT NULL,
                start_time      TEXT NOT NULL DEFAULT '',
                end_time        TEXT NOT NULL DEFAULT '',
                registration_open_at  TEXT NOT NULL DEFAULT '',
                registration_close_at TEXT NOT NULL DEFAULT '',
                people_max      INTEGER NOT NULL DEFAULT 0,
                description     TEXT NOT NULL DEFAULT '',
                location        TEXT NOT NULL DEFAULT '',
                contact         TEXT NOT NULL DEFAULT '',
                created_at      INTEGER NOT NULL,
                updated_at      INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS community_event_coorganizers (
                event_id        INTEGER NOT NULL,
                user_id         TEXT NOT NULL,
                uid             TEXT NOT NULL,
                display_name    TEXT NOT NULL DEFAULT '',
                added_at        INTEGER NOT NULL,
                PRIMARY KEY (event_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS community_event_registrations (
                event_id        INTEGER NOT NULL,
                user_id         TEXT NOT NULL,
                created_at      INTEGER NOT NULL,
                PRIMARY KEY (event_id, user_id)
            );

            CREATE INDEX IF NOT EXISTS idx_community_events_date
                ON community_events(date_iso, start_time);

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

            CREATE TABLE IF NOT EXISTS account_bans (
                uid          TEXT PRIMARY KEY,
                reason       TEXT NOT NULL,
                banned_by    TEXT NOT NULL DEFAULT '',
                banned_at    INTEGER NOT NULL
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

            CREATE TABLE IF NOT EXISTS personal_data_consents (
                uid          TEXT PRIMARY KEY,
                version      TEXT NOT NULL,
                accepted_at  INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS push_subscriptions (
                endpoint          TEXT PRIMARY KEY,
                user_id           TEXT NOT NULL,
                subscription_json TEXT NOT NULL,
                created_at        INTEGER NOT NULL,
                updated_at        INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS mobile_push_devices (
                token             TEXT PRIMARY KEY,
                user_id           TEXT NOT NULL,
                platform          TEXT NOT NULL DEFAULT 'android',
                created_at        INTEGER NOT NULL,
                updated_at        INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_mobile_push_devices_user
                ON mobile_push_devices(user_id, updated_at DESC);

            CREATE TABLE IF NOT EXISTS radar_alert_state (
                id          INTEGER PRIMARY KEY CHECK (id = 1),
                active      INTEGER NOT NULL,
                message     TEXT NOT NULL DEFAULT '',
                updated_at  INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS feedback_messages (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      TEXT NOT NULL DEFAULT '',
                message      TEXT NOT NULL,
                initiated_by TEXT NOT NULL DEFAULT 'user',
                initiator_name TEXT NOT NULL DEFAULT '',
                answer       TEXT NOT NULL DEFAULT '',
                answered_at  INTEGER NOT NULL DEFAULT 0,
                answered_by  TEXT NOT NULL DEFAULT '',
                user_hidden  INTEGER NOT NULL DEFAULT 0,
                admin_hidden INTEGER NOT NULL DEFAULT 0,
                is_read      INTEGER NOT NULL DEFAULT 0,
                created_at   INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS feedback_replies (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                feedback_id    INTEGER NOT NULL,
                sender_type    TEXT NOT NULL,
                sender_name    TEXT NOT NULL DEFAULT '',
                sender_user_id TEXT NOT NULL DEFAULT '',
                message        TEXT NOT NULL,
                created_at     INTEGER NOT NULL,
                FOREIGN KEY (feedback_id) REFERENCES feedback_messages(id)
            );

            CREATE INDEX IF NOT EXISTS idx_feedback_replies_feedback
                ON feedback_replies(feedback_id, created_at);

            CREATE TABLE IF NOT EXISTS sirius_request_stats (
                user_id      TEXT PRIMARY KEY,
                request_count INTEGER NOT NULL DEFAULT 0,
                updated_at   INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS token_verifications (
                user_id      TEXT PRIMARY KEY,
                token_hash   TEXT NOT NULL,
                verified_at  INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS prediction_markets (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                title             TEXT NOT NULL,
                description       TEXT NOT NULL DEFAULT '',
                market_type       TEXT NOT NULL,
                options_json      TEXT NOT NULL DEFAULT '[]',
                min_value         REAL,
                max_value         REAL,
                unit              TEXT NOT NULL DEFAULT '',
                end_at            INTEGER NOT NULL DEFAULT 0,
                betting_closes_at INTEGER NOT NULL DEFAULT 0,
                status            TEXT NOT NULL DEFAULT 'open',
                correct_option    TEXT NOT NULL DEFAULT '',
                correct_value     REAL,
                created_by        TEXT NOT NULL,
                created_at        INTEGER NOT NULL,
                resolved_at       INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS prediction_bets (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id   INTEGER NOT NULL,
                uid         TEXT NOT NULL,
                selection   TEXT NOT NULL DEFAULT '',
                value       REAL,
                amount      INTEGER NOT NULL,
                payout      INTEGER NOT NULL DEFAULT 0,
                created_at  INTEGER NOT NULL,
                FOREIGN KEY (market_id) REFERENCES prediction_markets(id)
            );

            CREATE INDEX IF NOT EXISTS idx_prediction_bets_market
                ON prediction_bets(market_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_prediction_bets_user
                ON prediction_bets(uid, market_id);

            CREATE TABLE IF NOT EXISTS referral_codes (
                uid          TEXT PRIMARY KEY,
                code         TEXT NOT NULL UNIQUE,
                created_at   INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS referrals (
                referred_uid TEXT PRIMARY KEY,
                referrer_uid TEXT NOT NULL,
                code         TEXT NOT NULL,
                rewarded_at  INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_referrals_referrer
                ON referrals(referrer_uid, rewarded_at DESC);

            CREATE TABLE IF NOT EXISTS app_usage_bonuses (
                uid          TEXT PRIMARY KEY,
                claimed_at   INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS promo_codes (
                code         TEXT PRIMARY KEY,
                coin_amount  INTEGER NOT NULL,
                max_uses     INTEGER NOT NULL,
                used_count   INTEGER NOT NULL DEFAULT 0,
                created_by   TEXT NOT NULL,
                created_at   INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS promo_redemptions (
                code         TEXT NOT NULL,
                uid          TEXT NOT NULL,
                redeemed_at  INTEGER NOT NULL,
                PRIMARY KEY (code, uid),
                FOREIGN KEY (code) REFERENCES promo_codes(code)
            );

            CREATE TABLE IF NOT EXISTS partner_account_links (
                partner          TEXT NOT NULL,
                external_user_id TEXT NOT NULL,
                uid              TEXT NOT NULL,
                linked_at        INTEGER NOT NULL,
                PRIMARY KEY (partner, external_user_id),
                UNIQUE (partner, uid)
            );

            CREATE TABLE IF NOT EXISTS partner_link_codes (
                code_hash  TEXT PRIMARY KEY,
                partner    TEXT NOT NULL,
                uid        TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS partner_coin_transactions (
                partner          TEXT NOT NULL,
                idempotency_key  TEXT NOT NULL,
                external_user_id TEXT NOT NULL,
                uid              TEXT NOT NULL,
                direction        TEXT NOT NULL,
                amount           INTEGER NOT NULL,
                reason           TEXT NOT NULL DEFAULT '',
                balance_after    INTEGER NOT NULL,
                created_at       INTEGER NOT NULL,
                PRIMARY KEY (partner, idempotency_key)
            );

            CREATE INDEX IF NOT EXISTS idx_partner_coin_transactions_uid
                ON partner_coin_transactions(uid, created_at DESC);

            CREATE TABLE IF NOT EXISTS partner_exchanges (
                partner          TEXT NOT NULL,
                idempotency_key  TEXT NOT NULL,
                uid              TEXT NOT NULL,
                external_user_id TEXT NOT NULL,
                direction        TEXT NOT NULL,
                coins            INTEGER NOT NULL,
                cookies          INTEGER NOT NULL,
                status           TEXT NOT NULL,
                local_balance    INTEGER,
                remote_balance   INTEGER,
                created_at       INTEGER NOT NULL,
                updated_at       INTEGER NOT NULL,
                PRIMARY KEY (partner, idempotency_key)
            );

            CREATE INDEX IF NOT EXISTS idx_partner_exchanges_pending
                ON partner_exchanges(partner, uid, status, updated_at DESC);

            CREATE TABLE IF NOT EXISTS partner_balance_cache (
                partner    TEXT NOT NULL,
                uid        TEXT NOT NULL,
                balance    INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (partner, uid)
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

        known_uid_cols = [r["name"] for r in conn.execute("PRAGMA table_info(known_uids)").fetchall()]
        if "team" not in known_uid_cols:
            conn.execute("ALTER TABLE known_uids ADD COLUMN team TEXT NOT NULL DEFAULT ''")

        coorganizer_cols = [r["name"] for r in conn.execute("PRAGMA table_info(community_event_coorganizers)").fetchall()]
        if "display_name" not in coorganizer_cols:
            conn.execute("ALTER TABLE community_event_coorganizers ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")

        community_event_cols = [r["name"] for r in conn.execute("PRAGMA table_info(community_events)").fetchall()]
        if "contact" not in community_event_cols:
            conn.execute("ALTER TABLE community_events ADD COLUMN contact TEXT NOT NULL DEFAULT ''")

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
        if "initiated_by" not in feedback_cols:
            conn.execute("ALTER TABLE feedback_messages ADD COLUMN initiated_by TEXT NOT NULL DEFAULT 'user'")
        if "initiator_name" not in feedback_cols:
            conn.execute("ALTER TABLE feedback_messages ADD COLUMN initiator_name TEXT NOT NULL DEFAULT ''")

        market_cols = [r["name"] for r in conn.execute("PRAGMA table_info(prediction_markets)").fetchall()]
        if "unit" not in market_cols:
            conn.execute("ALTER TABLE prediction_markets ADD COLUMN unit TEXT NOT NULL DEFAULT ''")

    retire_dronebet_markets()


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
        conn.execute("DELETE FROM token_verifications WHERE user_id=?", (user_id,))


def mark_token_verified(user_id: str, token: str):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO token_verifications (user_id, token_hash, verified_at)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 token_hash=excluded.token_hash,
                 verified_at=excluded.verified_at""",
            (user_id, hashlib.sha256(token.encode()).hexdigest(), int(time.time())),
        )


def is_token_verified(user_id: str, token: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT token_hash FROM token_verifications WHERE user_id=?", (user_id,)
        ).fetchone()
        return bool(row and secrets.compare_digest(row["token_hash"], hashlib.sha256(token.encode()).hexdigest()))


def save_login_credentials(user_id: str, email: str, password: str):
    with get_conn() as conn:
        conn.execute(
            """UPDATE users SET login_email=?, login_password=?, login_type='password'
               WHERE user_id=?""",
            (_encrypt(email), _encrypt(password), user_id),
        )


def save_email_code_login(user_id: str, email: str):
    """Remember only the email needed to send a new Sirius one-time code."""
    with get_conn() as conn:
        conn.execute(
            """UPDATE users SET login_email=?, login_password='', login_type='email_code'
               WHERE user_id=?""",
            (_encrypt(email), user_id),
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
            "watchlist", "custom_events", "notifications", "feedback_messages",
            "schedule_cache", "schedule_reminders", "event_snapshots",
            "sessions", "login_codes",
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
        "watchlist", "custom_events", "notifications", "feedback_messages",
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


def find_user_by_login_credentials(email: str, password: str) -> str | None:
    """Find a canonical Sirius account by its previously saved credentials.

    Temporary pre-login rows may have the same saved credentials but do not own
    the user's coins, permissions, or auto-registrations. They must never be
    used to restore a session.
    """
    normalized_email = email.strip().casefold()
    if not normalized_email or not password:
        return None
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT user_id, login_email, login_password, sirius_token FROM users "
            "WHERE user_id = uid AND uid != '' "
            "AND login_email != '' AND login_password != '' AND sirius_token != ''"
        ).fetchall()
    for row in rows:
        try:
            stored_email = _decrypt(row["login_email"])
            stored_password = _decrypt(row["login_password"])
        except Exception:
            continue
        if (
            stored_email.strip().casefold() == normalized_email
            and secrets.compare_digest(stored_password, password)
        ):
            return row["user_id"]
    return None


def get_login_email(user_id: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT login_email FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not row or not row["login_email"]:
            return None
        try:
            return _decrypt(row["login_email"])
        except Exception:
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


def take_active_watch(user_id: str, event_id: str) -> sqlite3.Row | None:
    """Remove and return a pending auto-registration exactly once.

    A successful manual registration must release its reserved coins rather than
    leave a completed auto-registration row behind.
    """
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM watchlist WHERE user_id=? AND event_id=? AND status='watching'",
            (user_id, event_id),
        ).fetchone()
        if row:
            conn.execute("DELETE FROM watchlist WHERE user_id=? AND event_id=?", (user_id, event_id))
        return row


def remove_watches_for_event(event_id: str) -> list[sqlite3.Row]:
    """Удаляет все автозаписи, связанные с удалённым пользовательским событием."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT user_id, event_id, coin_cost FROM watchlist WHERE event_id=?",
            (event_id,),
        ).fetchall()
        conn.execute("DELETE FROM watchlist WHERE event_id=?", (event_id,))
        return rows


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


def get_canonical_users_with_tokens() -> list[str]:
    """Return one current account row for every Sirius UID with a usable token."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT user_id FROM users
               WHERE uid != '' AND user_id = uid
                 AND sirius_token IS NOT NULL AND sirius_token != ''"""
        ).fetchall()
        return [r["user_id"] for r in rows]


def get_admin_users_with_tokens() -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT u.user_id
               FROM users u JOIN admins a ON a.uid=CASE WHEN u.uid!='' THEN u.uid ELSE u.user_id END
               WHERE u.sirius_token IS NOT NULL AND u.sirius_token!=''"""
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


# ---------- community events ----------

def _community_coorganizers(conn: sqlite3.Connection, event_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT c.user_id, c.uid, c.display_name, COALESCE(k.full_name, '') AS full_name
           FROM community_event_coorganizers c
           LEFT JOIN known_uids k ON k.user_id=c.user_id
           WHERE c.event_id=?
           ORDER BY c.added_at""",
        (event_id,),
    ).fetchall()


def add_community_event(
    owner_user_id: str,
    owner_uid: str,
    event_name: str,
    date_iso: str,
    start_time: str,
    end_time: str,
    registration_open_at: str,
    registration_close_at: str,
    people_max: int,
    description: str,
    location: str,
    coorganizers: list[tuple[str, str, str]],
    contact: str = "",
) -> int:
    now = int(time.time())
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO community_events
               (owner_user_id, owner_uid, event_name, date_iso, start_time, end_time,
                registration_open_at, registration_close_at, people_max, description,
                location, contact, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (owner_user_id, owner_uid, event_name, date_iso, start_time, end_time,
             registration_open_at, registration_close_at, people_max, description,
             location, contact, now, now),
        )
        event_id = int(cur.lastrowid)
        for entry in coorganizers:
            user_id, uid = entry[0], entry[1]
            display_name = entry[2] if len(entry) > 2 else ""
            if user_id != owner_user_id:
                conn.execute(
                    """INSERT OR IGNORE INTO community_event_coorganizers
                       (event_id, user_id, uid, display_name, added_at) VALUES (?, ?, ?, ?, ?)""",
                    (event_id, user_id, uid, display_name, now),
                )
        return event_id


def get_community_event(event_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            """SELECT e.*, COALESCE(k.full_name, '') AS owner_name,
                      (SELECT COUNT(*) FROM community_event_registrations r WHERE r.event_id=e.id) AS people_current
               FROM community_events e
               LEFT JOIN known_uids k ON k.user_id=e.owner_user_id
               WHERE e.id=?""",
            (event_id,),
        ).fetchone()


def get_community_coorganizers(event_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return _community_coorganizers(conn, event_id)


def get_community_events_for_date(user_id: str, date_iso: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT e.*, COALESCE(k.full_name, '') AS owner_name,
                      (SELECT COUNT(*) FROM community_event_registrations r WHERE r.event_id=e.id) AS people_current,
                      EXISTS(SELECT 1 FROM community_event_registrations r
                             WHERE r.event_id=e.id AND r.user_id=?) AS is_registered,
                      (e.owner_user_id=? OR EXISTS(SELECT 1 FROM community_event_coorganizers c
                                                    WHERE c.event_id=e.id AND c.user_id=?)) AS can_manage
               FROM community_events e
               LEFT JOIN known_uids k ON k.user_id=e.owner_user_id
               WHERE e.date_iso=?
               ORDER BY e.start_time, e.id""",
            (user_id, user_id, user_id, date_iso),
        ).fetchall()


def get_community_events_for_user(user_id: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT e.*, COALESCE(k.full_name, '') AS owner_name,
                      (SELECT COUNT(*) FROM community_event_registrations r WHERE r.event_id=e.id) AS people_current,
                      EXISTS(SELECT 1 FROM community_event_registrations r
                             WHERE r.event_id=e.id AND r.user_id=?) AS is_registered,
                      (e.owner_user_id=? OR EXISTS(SELECT 1 FROM community_event_coorganizers c
                                                    WHERE c.event_id=e.id AND c.user_id=?)) AS can_manage
               FROM community_events e
               LEFT JOIN known_uids k ON k.user_id=e.owner_user_id
               ORDER BY e.date_iso, e.start_time, e.id""",
            (user_id, user_id, user_id),
        ).fetchall()


def get_managed_community_events(user_id: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT e.*, COALESCE(k.full_name, '') AS owner_name,
                      (SELECT COUNT(*) FROM community_event_registrations r WHERE r.event_id=e.id) AS people_current
               FROM community_events e
               LEFT JOIN known_uids k ON k.user_id=e.owner_user_id
               WHERE e.owner_user_id=?
               ORDER BY e.date_iso, e.start_time, e.id""",
            (user_id,),
        ).fetchall()


def can_manage_community_event(event_id: int, user_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT 1 FROM community_events e
               WHERE e.id=? AND (e.owner_user_id=? OR EXISTS(
                   SELECT 1 FROM community_event_coorganizers c
                   WHERE c.event_id=e.id AND c.user_id=?))""",
            (event_id, user_id, user_id),
        ).fetchone()
        return bool(row)


def update_community_event(
    event_id: int,
    event_name: str,
    date_iso: str,
    start_time: str,
    end_time: str,
    registration_open_at: str,
    registration_close_at: str,
    people_max: int,
    description: str,
    location: str,
    coorganizers: list[tuple[str, str, str]],
    contact: str = "",
) -> bool:
    now = int(time.time())
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE community_events
               SET event_name=?, date_iso=?, start_time=?, end_time=?,
                   registration_open_at=?, registration_close_at=?, people_max=?,
                   description=?, location=?, contact=?, updated_at=?
               WHERE id=?""",
            (event_name, date_iso, start_time, end_time, registration_open_at,
             registration_close_at, people_max, description, location, contact, now, event_id),
        )
        if cur.rowcount != 1:
            return False
        conn.execute("DELETE FROM community_event_coorganizers WHERE event_id=?", (event_id,))
        owner = conn.execute("SELECT owner_user_id FROM community_events WHERE id=?", (event_id,)).fetchone()
        for entry in coorganizers:
            user_id, uid = entry[0], entry[1]
            display_name = entry[2] if len(entry) > 2 else ""
            if owner and user_id != owner["owner_user_id"]:
                conn.execute(
                    """INSERT OR IGNORE INTO community_event_coorganizers
                       (event_id, user_id, uid, display_name, added_at) VALUES (?, ?, ?, ?, ?)""",
                    (event_id, user_id, uid, display_name, now),
                )
        return True


def delete_community_event(event_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM community_event_registrations WHERE event_id=?", (event_id,))
        conn.execute("DELETE FROM community_event_coorganizers WHERE event_id=?", (event_id,))
        conn.execute("DELETE FROM community_events WHERE id=?", (event_id,))


def get_community_event_participants(event_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT r.user_id, r.created_at, COALESCE(k.full_name, '') AS full_name, COALESCE(k.uid, '') AS uid
               FROM community_event_registrations r
               LEFT JOIN known_uids k ON k.user_id=r.user_id
               WHERE r.event_id=?
               ORDER BY r.created_at, r.user_id""",
            (event_id,),
        ).fetchall()


def add_community_registration(event_id: int, user_id: str) -> tuple[bool, str]:
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        event = conn.execute("SELECT people_max FROM community_events WHERE id=?", (event_id,)).fetchone()
        if not event:
            return False, "not_found"
        exists = conn.execute(
            "SELECT 1 FROM community_event_registrations WHERE event_id=? AND user_id=?",
            (event_id, user_id),
        ).fetchone()
        if exists:
            return True, "already_registered"
        current = conn.execute(
            "SELECT COUNT(*) AS count FROM community_event_registrations WHERE event_id=?", (event_id,)
        ).fetchone()["count"]
        if event["people_max"] > 0 and current >= event["people_max"]:
            return False, "full"
        conn.execute(
            "INSERT INTO community_event_registrations (event_id, user_id, created_at) VALUES (?, ?, ?)",
            (event_id, user_id, int(time.time())),
        )
        return True, "registered"


def remove_community_registration(event_id: int, user_id: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM community_event_registrations WHERE event_id=? AND user_id=?",
            (event_id, user_id),
        )


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
            """INSERT INTO feedback_messages
               (user_id, message, initiated_by, initiator_name, created_at)
               VALUES (?, ?, 'user', '', ?)""",
            (user_id or "", message, int(time.time())),
        )


def create_admin_feedback(user_id: str, message: str, admin_name: str) -> int:
    with get_conn() as conn:
        cursor = conn.execute(
            """INSERT INTO feedback_messages
               (user_id, message, initiated_by, initiator_name, is_read, created_at)
               VALUES (?, ?, 'admin', ?, 1, ?)""",
            (user_id, message, admin_name, int(time.time())),
        )
        return int(cursor.lastrowid)


def get_feedback_messages(limit: int = 200) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT f.id, f.user_id, f.message, f.initiated_by, f.initiator_name,
                      f.answer, f.answered_at, f.answered_by, f.is_read, f.created_at,
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
            """SELECT id, message, initiated_by, initiator_name,
                      answer, answered_at, answered_by, is_read, created_at
               FROM feedback_messages
               WHERE user_id=? AND user_hidden=0
               ORDER BY created_at DESC
               LIMIT ?""",
            (user_id, limit),
        ).fetchall()


def get_user_feedback_message(user_id: str, feedback_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM feedback_messages WHERE id=? AND user_id=? AND user_hidden=0",
            (feedback_id, user_id),
        ).fetchone()


def get_feedback_replies(feedback_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            """SELECT sender_type, sender_name, sender_user_id, message, created_at
               FROM feedback_replies WHERE feedback_id=? ORDER BY created_at, id""",
            (feedback_id,),
        ).fetchall()


def add_feedback_reply(
    feedback_id: int,
    sender_type: str,
    sender_name: str,
    sender_user_id: str,
    message: str,
) -> sqlite3.Row | None:
    now = int(time.time())
    with get_conn() as conn:
        row = conn.execute("SELECT user_id FROM feedback_messages WHERE id=?", (feedback_id,)).fetchone()
        if not row:
            return None
        conn.execute(
            """INSERT INTO feedback_replies
               (feedback_id, sender_type, sender_name, sender_user_id, message, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (feedback_id, sender_type, sender_name, sender_user_id, message, now),
        )
        conn.execute("UPDATE feedback_messages SET is_read=? WHERE id=?", (0 if sender_type == "user" else 1, feedback_id))
        return row


def get_known_name(uid: str) -> str:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT full_name FROM known_uids WHERE uid=? OR user_id=? ORDER BY updated_at DESC LIMIT 1",
            (uid, uid),
        ).fetchone()
        return row["full_name"] if row and row["full_name"] else ""


def resolve_known_recipient(value: str) -> list[sqlite3.Row]:
    """Resolve an exact UID or full name without guessing between people with the same name."""
    needle = (value or "").strip()
    if not needle:
        return []
    with get_conn() as conn:
        direct = conn.execute(
            "SELECT uid, user_id, full_name, team FROM known_uids WHERE uid=?", (needle,)
        ).fetchall()
        if direct:
            return direct
        rows = conn.execute(
            "SELECT uid, user_id, full_name, team FROM known_uids WHERE full_name != ''"
        ).fetchall()
        return [row for row in rows if str(row["full_name"]).strip().casefold() == needle.casefold()]


# ---------- Sirius request statistics ----------

def increment_sirius_request(user_id: str) -> None:
    if not user_id:
        return
    now = int(time.time())
    try:
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO sirius_request_stats (user_id, request_count, updated_at)
                   VALUES (?, 1, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     request_count=sirius_request_stats.request_count+1,
                     updated_at=excluded.updated_at""",
                (user_id, now),
            )
    except sqlite3.Error as exc:
        # Statistics must never delay or cancel a Sirius request, including during a DB migration.
        import logging
        logging.getLogger("storage").warning("Could not record Sirius request for %s: %s", user_id, exc)


def get_sirius_request_count(user_id: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT request_count FROM sirius_request_stats WHERE user_id=?",
            (user_id,),
        ).fetchone()
        return int(row["request_count"]) if row else 0


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
        conn.execute("DELETE FROM feedback_replies WHERE feedback_id=?", (feedback_id,))
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


def save_mobile_push_device(user_id: str, token: str, platform: str = "android") -> None:
    now = int(time.time())
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO mobile_push_devices (token, user_id, platform, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(token) DO UPDATE SET
                 user_id=excluded.user_id,
                 platform=excluded.platform,
                 updated_at=excluded.updated_at""",
            (token, user_id, platform, now, now),
        )


def delete_mobile_push_device(token: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM mobile_push_devices WHERE token=?", (token,))


def get_mobile_push_devices(user_id: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT token, platform FROM mobile_push_devices WHERE user_id=?", (user_id,)
        ).fetchall()


def get_radar_alert_state() -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT active, message, updated_at FROM radar_alert_state WHERE id=1").fetchone()


def set_radar_alert_state(active: bool, message: str) -> None:
    now = int(time.time())
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO radar_alert_state (id, active, message, updated_at)
               VALUES (1, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 active=excluded.active, message=excluded.message, updated_at=excluded.updated_at""",
            (int(active), message, now),
        )


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


def claim_app_usage_bonus(uid: str, amount: int = 2) -> bool:
    """Credits the Android-app bonus only once per Sirius account."""
    now = int(time.time())
    with get_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO app_usage_bonuses (uid, claimed_at) VALUES (?, ?)",
                (uid, now),
            )
        except sqlite3.IntegrityError:
            return False
        conn.execute(
            "UPDATE sirius_coins SET coins=coins+?, updated_at=? WHERE uid=?",
            (amount, now, uid),
        )
        return True


def has_claimed_app_usage_bonus(uid: str) -> bool:
    with get_conn() as conn:
        return bool(conn.execute(
            "SELECT 1 FROM app_usage_bonuses WHERE uid=?", (uid,)
        ).fetchone())


# ---------- partner wallets ----------

def _normalize_partner_code(code: str) -> str:
    return "".join(char for char in (code or "").upper() if char.isalnum())


def _partner_code_hash(code: str) -> str:
    return hashlib.sha256(_normalize_partner_code(code).encode()).hexdigest()


def create_partner_link_code(partner: str, uid: str, ttl_seconds: int = 600) -> tuple[str, int]:
    """Create a short-lived, one-use code to link an external partner account."""
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    now = int(time.time())
    expires_at = now + max(60, int(ttl_seconds))
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM partner_link_codes WHERE partner=? AND (uid=? OR expires_at<?)",
            (partner, uid, now),
        )
        for _ in range(10):
            code = "".join(secrets.choice(alphabet) for _ in range(10))
            try:
                conn.execute(
                    """INSERT INTO partner_link_codes (code_hash, partner, uid, created_at, expires_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (_partner_code_hash(code), partner, uid, now, expires_at),
                )
                return code, expires_at
            except sqlite3.IntegrityError:
                continue
    raise RuntimeError("Не удалось создать код привязки")


def get_partner_link(partner: str, external_user_id: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM partner_account_links WHERE partner=? AND external_user_id=?",
            (partner, external_user_id),
        ).fetchone()


def get_partner_link_for_uid(partner: str, uid: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM partner_account_links WHERE partner=? AND uid=?",
            (partner, uid),
        ).fetchone()


def link_partner_account(partner: str, uid: str, external_user_id: str) -> tuple[bool, str]:
    """Persist a link accepted by the remote partner, preserving one-to-one ownership."""
    uid = str(uid or "").strip()
    external_user_id = str(external_user_id or "").strip()
    if not uid or not external_user_id or len(external_user_id) > 128:
        return False, "invalid_account"
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        by_external = conn.execute(
            "SELECT uid FROM partner_account_links WHERE partner=? AND external_user_id=?",
            (partner, external_user_id),
        ).fetchone()
        if by_external and by_external["uid"] != uid:
            return False, "drone_account_already_linked"
        by_uid = conn.execute(
            "SELECT external_user_id FROM partner_account_links WHERE partner=? AND uid=?",
            (partner, uid),
        ).fetchone()
        if by_uid and by_uid["external_user_id"] != external_user_id:
            return False, "sirius_account_already_linked"
        if not by_external:
            conn.execute(
                "INSERT INTO partner_account_links (partner, external_user_id, uid, linked_at) VALUES (?, ?, ?, ?)",
                (partner, external_user_id, uid, int(time.time())),
            )
        return True, "linked"


def begin_partner_exchange(
    partner: str,
    uid: str,
    external_user_id: str,
    direction: str,
    coins: int,
    cookies: int,
    idempotency_key: str,
) -> tuple[dict | None, str]:
    """Create or resume one exchange. A pending exchange is always resumed first."""
    if direction not in {"coins_to_cookies", "cookies_to_coins"}:
        return None, "invalid_direction"
    if not isinstance(coins, int) or coins < 1 or cookies != coins * config.DRONEBET_COOKIE_RATE:
        return None, "invalid_amount"
    if not isinstance(idempotency_key, str) or not 12 <= len(idempotency_key) <= 128:
        return None, "invalid_idempotency_key"
    now = int(time.time())
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM partner_exchanges WHERE partner=? AND idempotency_key=?",
            (partner, idempotency_key),
        ).fetchone()
        if existing:
            same = all((
                existing["uid"] == uid,
                existing["external_user_id"] == external_user_id,
                existing["direction"] == direction,
                existing["coins"] == coins,
                existing["cookies"] == cookies,
            ))
            return (dict(existing), "existing") if same else (None, "idempotency_key_conflict")
        pending = conn.execute(
            """SELECT * FROM partner_exchanges
               WHERE partner=? AND uid=? AND direction=? AND coins=? AND cookies=? AND status='pending'
               ORDER BY created_at DESC LIMIT 1""",
            (partner, uid, direction, coins, cookies),
        ).fetchone()
        if pending:
            return dict(pending), "pending"
        conn.execute(
            """INSERT INTO partner_exchanges
               (partner, idempotency_key, uid, external_user_id, direction, coins, cookies, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (partner, idempotency_key, uid, external_user_id, direction, coins, cookies, now, now),
        )
        return {
            "partner": partner, "idempotency_key": idempotency_key, "uid": uid,
            "external_user_id": external_user_id, "direction": direction, "coins": coins,
            "cookies": cookies, "status": "pending", "local_balance": None, "remote_balance": None,
        }, "created"


def complete_partner_exchange(partner: str, idempotency_key: str, local_balance: int, remote_balance: int | None) -> None:
    with get_conn() as conn:
        conn.execute(
            """UPDATE partner_exchanges SET status='completed', local_balance=?, remote_balance=?, updated_at=?
               WHERE partner=? AND idempotency_key=?""",
            (local_balance, remote_balance, int(time.time()), partner, idempotency_key),
        )


def cache_partner_balance(partner: str, uid: str, balance: int) -> None:
    if not isinstance(balance, int) or balance < 0:
        return
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO partner_balance_cache (partner, uid, balance, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(partner, uid) DO UPDATE SET balance=excluded.balance, updated_at=excluded.updated_at""",
            (partner, uid, balance, int(time.time())),
        )


def get_partner_balance_cache(partner: str, uid: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT balance, updated_at FROM partner_balance_cache WHERE partner=? AND uid=?",
            (partner, uid),
        ).fetchone()


def fail_partner_exchange(partner: str, idempotency_key: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE partner_exchanges SET status='failed', updated_at=? WHERE partner=? AND idempotency_key=?",
            (int(time.time()), partner, idempotency_key),
        )


def claim_partner_link_code(partner: str, code: str, external_user_id: str) -> tuple[bool, str, str | None]:
    """Consume a code and establish a strict one-to-one partner account link."""
    external_user_id = str(external_user_id or "").strip()
    normalized = _normalize_partner_code(code)
    if len(normalized) < 8 or not external_user_id or len(external_user_id) > 128:
        return False, "invalid_link_code", None
    now = int(time.time())
    with get_conn() as conn:
        conn.execute("DELETE FROM partner_link_codes WHERE expires_at<?", (now,))
        row = conn.execute(
            "SELECT * FROM partner_link_codes WHERE code_hash=? AND partner=? AND expires_at>=?",
            (_partner_code_hash(normalized), partner, now),
        ).fetchone()
        if not row:
            return False, "invalid_link_code", None
        existing_external = conn.execute(
            "SELECT uid FROM partner_account_links WHERE partner=? AND external_user_id=?",
            (partner, external_user_id),
        ).fetchone()
        if existing_external and existing_external["uid"] != row["uid"]:
            return False, "external_account_already_linked", None
        existing_uid = conn.execute(
            "SELECT external_user_id FROM partner_account_links WHERE partner=? AND uid=?",
            (partner, row["uid"]),
        ).fetchone()
        if existing_uid and existing_uid["external_user_id"] != external_user_id:
            return False, "sirius_account_already_linked", None
        conn.execute(
            """INSERT INTO partner_account_links (partner, external_user_id, uid, linked_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(partner, external_user_id) DO NOTHING""",
            (partner, external_user_id, row["uid"], now),
        )
        conn.execute("DELETE FROM partner_link_codes WHERE code_hash=?", (row["code_hash"],))
        return True, "linked", row["uid"]


def partner_coin_transaction(
    partner: str,
    external_user_id: str,
    direction: str,
    amount: int,
    idempotency_key: str,
    reason: str = "",
) -> dict:
    """Atomically credit or debit a linked Sirius account exactly once."""
    if direction not in {"credit", "debit"}:
        return {"ok": False, "code": "invalid_direction"}
    if not isinstance(amount, int) or amount < 1 or amount > 1_000_000:
        return {"ok": False, "code": "invalid_amount"}
    if not isinstance(idempotency_key, str) or not 12 <= len(idempotency_key) <= 128:
        return {"ok": False, "code": "invalid_idempotency_key"}
    external_user_id = str(external_user_id or "").strip()
    if not external_user_id or len(external_user_id) > 128:
        return {"ok": False, "code": "invalid_external_user_id"}

    now = int(time.time())
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        previous = conn.execute(
            "SELECT * FROM partner_coin_transactions WHERE partner=? AND idempotency_key=?",
            (partner, idempotency_key),
        ).fetchone()
        if previous:
            same_operation = (
                previous["external_user_id"] == external_user_id
                and previous["direction"] == direction
                and previous["amount"] == amount
            )
            if not same_operation:
                return {"ok": False, "code": "idempotency_key_conflict"}
            return {
                "ok": True, "replayed": True, "uid": previous["uid"],
                "balance": previous["balance_after"], "direction": previous["direction"],
                "amount": previous["amount"],
            }

        link = conn.execute(
            "SELECT uid FROM partner_account_links WHERE partner=? AND external_user_id=?",
            (partner, external_user_id),
        ).fetchone()
        if not link:
            return {"ok": False, "code": "account_not_linked"}
        uid = link["uid"]
        coins = conn.execute(
            "SELECT coins, reserved_coins FROM sirius_coins WHERE uid=?", (uid,)
        ).fetchone()
        total = coins["coins"] if coins else 0
        reserved = coins["reserved_coins"] if coins else 0
        available = total - reserved
        if direction == "debit" and available < amount:
            return {"ok": False, "code": "insufficient_coins", "balance": available}

        new_total = total + amount if direction == "credit" else total - amount
        if coins:
            conn.execute(
                "UPDATE sirius_coins SET coins=?, updated_at=? WHERE uid=?",
                (new_total, now, uid),
            )
        else:
            conn.execute(
                """INSERT INTO sirius_coins (uid, coins, reserved_coins, created_at, updated_at)
                   VALUES (?, ?, 0, ?, ?)""",
                (uid, new_total, now, now),
            )
        balance = new_total - reserved
        conn.execute(
            """INSERT INTO partner_coin_transactions
               (partner, idempotency_key, external_user_id, uid, direction, amount, reason, balance_after, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (partner, idempotency_key, external_user_id, uid, direction, amount, reason[:300], balance, now),
        )
        return {
            "ok": True, "replayed": False, "uid": uid, "balance": balance,
            "direction": direction, "amount": amount,
        }


# ---------- promo codes ----------

def normalize_promo_code(code: str) -> str:
    return "".join(char for char in (code or "").upper() if char.isalnum())[:32]


def create_promo_code(code: str, coin_amount: int, max_uses: int, created_by: str) -> str | None:
    code = normalize_promo_code(code)
    if not code or coin_amount <= 0 or max_uses <= 0:
        return None
    with get_conn() as conn:
        try:
            conn.execute(
                """INSERT INTO promo_codes (code, coin_amount, max_uses, created_by, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (code, coin_amount, max_uses, created_by, int(time.time())),
            )
        except sqlite3.IntegrityError:
            return None
    return code


def get_promo_codes() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM promo_codes ORDER BY created_at DESC"
        ).fetchall()


def redeem_promo_code(code: str, uid: str) -> tuple[bool, str, int]:
    code = normalize_promo_code(code)
    if not code:
        return False, "Введи промокод", 0
    now = int(time.time())
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        promo = conn.execute(
            "SELECT coin_amount, max_uses, used_count FROM promo_codes WHERE code=?", (code,)
        ).fetchone()
        if not promo:
            return False, "Промокод не найден", 0
        if conn.execute(
            "SELECT 1 FROM promo_redemptions WHERE code=? AND uid=?", (code, uid)
        ).fetchone():
            return False, "Ты уже использовал этот промокод", 0
        if promo["used_count"] >= promo["max_uses"]:
            return False, "У промокода закончились активации", 0

        conn.execute(
            "INSERT INTO promo_redemptions (code, uid, redeemed_at) VALUES (?, ?, ?)",
            (code, uid, now),
        )
        conn.execute("UPDATE promo_codes SET used_count=used_count+1 WHERE code=?", (code,))
        row = conn.execute("SELECT coins FROM sirius_coins WHERE uid=?", (uid,)).fetchone()
        if row:
            new_balance = row["coins"] + promo["coin_amount"]
            conn.execute(
                "UPDATE sirius_coins SET coins=?, updated_at=? WHERE uid=?",
                (new_balance, now, uid),
            )
        else:
            new_balance = STARTING_COINS + promo["coin_amount"]
            conn.execute(
                """INSERT INTO sirius_coins (uid, coins, reserved_coins, created_at, updated_at)
                   VALUES (?, ?, 0, ?, ?)""",
                (uid, new_balance, now, now),
            )
        return True, f"Промокод активирован: +{promo['coin_amount']} Сириус Коинов", new_balance


# ---------- referrals ----------

def normalize_referral_code(code: str) -> str:
    return "".join(char for char in (code or "").upper() if char.isalnum())[:12]


def get_or_create_referral_code(uid: str) -> str:
    with get_conn() as conn:
        row = conn.execute("SELECT code FROM referral_codes WHERE uid=?", (uid,)).fetchone()
        if row:
            return row["code"]
        for _ in range(20):
            code = secrets.token_urlsafe(6).replace("-", "").replace("_", "").upper()[:8]
            try:
                conn.execute(
                    "INSERT INTO referral_codes (uid, code, created_at) VALUES (?, ?, ?)",
                    (uid, code, int(time.time())),
                )
                return code
            except sqlite3.IntegrityError:
                continue
    raise RuntimeError("Не удалось создать реферальный код")


def apply_referral(code: str, referred_uid: str, reward: int = 5) -> bool:
    """One successful registration may use one code; both accounts receive the reward."""
    code = normalize_referral_code(code)
    if not code:
        return False
    now = int(time.time())
    with get_conn() as conn:
        referrer = conn.execute("SELECT uid FROM referral_codes WHERE code=?", (code,)).fetchone()
        if not referrer or referrer["uid"] == referred_uid:
            return False
        try:
            conn.execute(
                "INSERT INTO referrals (referred_uid, referrer_uid, code, rewarded_at) VALUES (?, ?, ?, ?)",
                (referred_uid, referrer["uid"], code, now),
            )
        except sqlite3.IntegrityError:
            return False
        for uid in (referrer["uid"], referred_uid):
            conn.execute(
                "UPDATE sirius_coins SET coins=coins+?, updated_at=? WHERE uid=?",
                (reward, now, uid),
            )
        return True


def get_referral_count(uid: str) -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM referrals WHERE referrer_uid=?", (uid,)).fetchone()
        return int(row["count"])


def get_referrer_uid(referred_uid: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT referrer_uid FROM referrals WHERE referred_uid=?", (referred_uid,)).fetchone()
        return row["referrer_uid"] if row else None


def create_prediction_market(
    title: str,
    description: str,
    market_type: str,
    options_json: str,
    min_value: float | None,
    max_value: float | None,
    end_at: int,
    betting_closes_at: int,
    created_by: str,
    unit: str = "",
    correct_value: float | None = None,
) -> int:
    now = int(time.time())
    with get_conn() as conn:
        cursor = conn.execute(
            """INSERT INTO prediction_markets
               (title, description, market_type, options_json, min_value, max_value, unit,
                end_at, betting_closes_at, correct_value, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (title, description, market_type, options_json, min_value, max_value, unit,
             end_at, betting_closes_at, correct_value, created_by, now),
        )
        return int(cursor.lastrowid)


def get_prediction_market(market_id: int) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM prediction_markets WHERE id=?", (market_id,)).fetchone()


def get_prediction_markets() -> list[sqlite3.Row]:
    with get_conn() as conn:
        query = """SELECT * FROM prediction_markets
                   WHERE created_by != 'dronebet'
                   ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, created_at DESC"""
        return conn.execute(query).fetchall()


def get_prediction_bets(market_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM prediction_bets WHERE market_id=? ORDER BY created_at, id",
            (market_id,),
        ).fetchall()


def place_prediction_bet(uid: str, market_id: int, selection: str, value: float | None, amount: int) -> tuple[bool, str, int]:
    """Deducts a bet and appends it to the market in one SQLite transaction."""
    if amount < 1:
        return False, "Количество должно быть больше нуля", 0
    now = int(time.time())
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        market = conn.execute("SELECT * FROM prediction_markets WHERE id=?", (market_id,)).fetchone()
        if not market:
            return False, "Рынок не найден", 0
        if market["status"] != "open":
            return False, "Этот рынок уже завершён", 0
        if market["end_at"] and now >= market["end_at"]:
            return False, "Время события уже наступило", 0
        if market["betting_closes_at"] and now >= market["betting_closes_at"]:
            return False, "Приём ставок уже закрыт", 0

        coin_row = conn.execute(
            "SELECT coins, reserved_coins FROM sirius_coins WHERE uid=?", (uid,)
        ).fetchone()
        available = (coin_row["coins"] - coin_row["reserved_coins"]) if coin_row else 0
        if available < amount:
            return False, f"Недостаточно коинов. Доступно: {available}", available

        conn.execute(
            "UPDATE sirius_coins SET coins=coins-?, updated_at=? WHERE uid=?",
            (amount, now, uid),
        )
        conn.execute(
            """INSERT INTO prediction_bets (market_id, uid, selection, value, amount, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (market_id, uid, selection, value, amount, now),
        )
        return True, "", available - amount


def _split_prediction_pool(bets: list[sqlite3.Row], weights: list[float], total_pool: int) -> dict[int, int]:
    total_weight = sum(weights)
    if total_pool <= 0 or total_weight <= 0:
        return {int(bet["id"]): 0 for bet in bets}
    raw = [total_pool * weight / total_weight for weight in weights]
    payouts = [math.floor(value) for value in raw]
    remaining = total_pool - sum(payouts)
    order = sorted(range(len(bets)), key=lambda index: (raw[index] - payouts[index], -bets[index]["id"]), reverse=True)
    for index in order[:remaining]:
        payouts[index] += 1
    return {int(bet["id"]): payout for bet, payout in zip(bets, payouts)}


def resolve_prediction_market(market_id: int, correct_option: str = "", correct_value: float | None = None) -> tuple[bool, str, list[tuple[str, int]]]:
    """Settles an open market exactly once and returns non-zero payouts by user."""
    now = int(time.time())
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        market = conn.execute("SELECT * FROM prediction_markets WHERE id=?", (market_id,)).fetchone()
        if not market:
            return False, "Рынок не найден", []
        if market["status"] != "open":
            return False, "Рынок уже рассчитан или отменён", []
        bets = conn.execute(
            "SELECT * FROM prediction_bets WHERE market_id=? ORDER BY id", (market_id,)
        ).fetchall()
        total_pool = sum(int(bet["amount"]) for bet in bets)

        if market["market_type"] == "choice":
            winners = [bet for bet in bets if bet["selection"] == correct_option]
            payouts = _split_prediction_pool(winners, [float(bet["amount"]) for bet in winners], total_pool)
            result_option, result_value = correct_option, None
        else:
            if correct_value is None:
                return False, "Укажи правильное число", []
            span = max(float(market["max_value"]) - float(market["min_value"]), 1.0)
            weights = []
            for bet in bets:
                distance = abs(float(bet["value"]) - correct_value) / span
                closeness = max(0.15, 1 - distance)
                weights.append(float(bet["amount"]) * closeness)
            payouts = _split_prediction_pool(bets, weights, total_pool)
            result_option, result_value = "", correct_value

        totals_by_user: dict[str, int] = {bet["uid"]: 0 for bet in bets}
        for bet in bets:
            payout = payouts.get(int(bet["id"]), 0)
            conn.execute("UPDATE prediction_bets SET payout=? WHERE id=?", (payout, bet["id"]))
            if payout:
                totals_by_user[bet["uid"]] = totals_by_user.get(bet["uid"], 0) + payout
        for uid, payout in totals_by_user.items():
            if payout:
                conn.execute(
                    "UPDATE sirius_coins SET coins=coins+?, updated_at=? WHERE uid=?",
                    (payout, now, uid),
                )
        conn.execute(
            """UPDATE prediction_markets
               SET status='resolved', correct_option=?, correct_value=?, resolved_at=?
               WHERE id=?""",
            (result_option, result_value, now, market_id),
        )
        return True, "", list(totals_by_user.items())


def cancel_prediction_market(market_id: int) -> tuple[bool, str, list[tuple[str, int]]]:
    """Cancels an open market and returns every stake."""
    now = int(time.time())
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        market = conn.execute("SELECT status FROM prediction_markets WHERE id=?", (market_id,)).fetchone()
        if not market:
            return False, "Рынок не найден", []
        if market["status"] != "open":
            return False, "Можно отменить только открытый рынок", []
        bets = conn.execute("SELECT uid, amount FROM prediction_bets WHERE market_id=?", (market_id,)).fetchall()
        refunds: dict[str, int] = {}
        for bet in bets:
            refunds[bet["uid"]] = refunds.get(bet["uid"], 0) + int(bet["amount"])
        for uid, refund in refunds.items():
            conn.execute(
                "UPDATE sirius_coins SET coins=coins+?, updated_at=? WHERE uid=?",
                (refund, now, uid),
            )
        conn.execute("UPDATE prediction_markets SET status='cancelled', resolved_at=? WHERE id=?", (now, market_id))
        return True, "", list(refunds.items())


def delete_prediction_market(market_id: int) -> tuple[bool, str]:
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        market = conn.execute("SELECT status FROM prediction_markets WHERE id=?", (market_id,)).fetchone()
        if not market:
            return False, "Рынок не найден"
        bet_count = conn.execute("SELECT COUNT(*) AS count FROM prediction_bets WHERE market_id=?", (market_id,)).fetchone()["count"]
        if market["status"] == "open" and bet_count:
            return False, "Сначала отмени рынок: ставки нужно вернуть участникам"
        conn.execute("DELETE FROM prediction_bets WHERE market_id=?", (market_id,))
        conn.execute("DELETE FROM prediction_markets WHERE id=?", (market_id,))
        return True, ""


def retire_dronebet_markets() -> None:
    """Cancel old in-house DroneBet markets and return every open stake once."""
    now = int(time.time())
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        markets = conn.execute(
            "SELECT id FROM prediction_markets WHERE created_by='dronebet' AND status='open'"
        ).fetchall()
        for market in markets:
            bets = conn.execute(
                "SELECT uid, SUM(amount) AS amount FROM prediction_bets WHERE market_id=? GROUP BY uid",
                (market["id"],),
            ).fetchall()
            for bet in bets:
                conn.execute(
                    "UPDATE sirius_coins SET coins=coins+?, updated_at=? WHERE uid=?",
                    (int(bet["amount"]), now, bet["uid"]),
                )
            conn.execute(
                "UPDATE prediction_markets SET status='cancelled', resolved_at=? WHERE id=?",
                (now, market["id"]),
            )


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

def save_known_uid(uid: str, user_id: str, full_name: str = "", team: str = ""):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO known_uids (uid, user_id, full_name, team, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(uid) DO UPDATE SET
                 user_id=excluded.user_id,
                 full_name=excluded.full_name,
                 team=CASE WHEN excluded.team != '' THEN excluded.team ELSE known_uids.team END,
                 updated_at=excluded.updated_at""",
            (uid, user_id, full_name, team, int(time.time())),
        )


def update_known_team(user_id: str, team: str):
    team = (team or "").strip()
    if not team:
        return
    with get_conn() as conn:
        conn.execute(
            "UPDATE known_uids SET team=?, updated_at=? WHERE user_id=? OR uid=?",
            (team, int(time.time()), user_id, user_id),
        )


def get_known_team(user_id: str) -> str:
    with get_conn() as conn:
        row = conn.execute(
            """SELECT team FROM known_uids
               WHERE user_id=? OR uid=?
               ORDER BY updated_at DESC LIMIT 1""",
            (user_id, user_id),
        ).fetchone()
        return str(row["team"] or "") if row else ""


def get_all_known_uids() -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT k.uid, k.full_name, k.team, COALESCE(c.coins, 0) as coins, "
            "COALESCE(c.reserved_coins, 0) as reserved_coins, "
            "COALESCE(t.trust_level, 2) as trust_level, "
            "CASE WHEN a.uid IS NULL THEN 0 ELSE 1 END as is_admin, "
            "CASE WHEN b.uid IS NULL THEN 0 ELSE 1 END as is_banned, "
            "COALESCE(b.reason, '') AS ban_reason, k.updated_at "
            "FROM known_uids k LEFT JOIN sirius_coins c ON k.uid = c.uid "
            "LEFT JOIN account_trust t ON k.uid = t.uid "
            "LEFT JOIN admins a ON k.uid = a.uid "
            "LEFT JOIN account_bans b ON k.uid = b.uid "
            "ORDER BY k.updated_at DESC"
        ).fetchall()


def get_admin_user_profile(uid: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            """SELECT k.uid, k.user_id, k.full_name, k.team, k.updated_at,
                      u.created_at, u.login_type,
                      COALESCE(c.coins, 0) AS coins,
                      COALESCE(c.reserved_coins, 0) AS reserved_coins,
                      COALESCE(t.trust_level, 2) AS trust_level,
                      CASE WHEN a.uid IS NULL THEN 0 ELSE 1 END AS is_admin,
                      CASE WHEN b.uid IS NULL THEN 0 ELSE 1 END AS is_banned,
                      COALESCE(b.reason, '') AS ban_reason,
                      COALESCE((SELECT MAX(s.last_active) FROM sessions s WHERE s.user_id=k.user_id), 0) AS last_active,
                      (SELECT COUNT(*) FROM watchlist w WHERE w.user_id=k.user_id AND w.status='watching') AS watching_count,
                      (SELECT COUNT(*) FROM watchlist w WHERE w.user_id=k.user_id AND w.status='registered') AS registered_count,
                      (SELECT COUNT(*) FROM schedule_reminders r WHERE r.user_id=k.user_id) AS reminder_count
               FROM known_uids k
               LEFT JOIN users u ON u.user_id=k.user_id
               LEFT JOIN sirius_coins c ON c.uid=k.uid
               LEFT JOIN account_trust t ON t.uid=k.uid
               LEFT JOIN admins a ON a.uid=k.uid
               LEFT JOIN account_bans b ON b.uid=k.uid
               WHERE k.uid=?""",
            (uid,),
        ).fetchone()


# ---------- account bans ----------

def get_account_ban(uid: str) -> sqlite3.Row | None:
    if not uid:
        return None
    with get_conn() as conn:
        return conn.execute(
            "SELECT uid, reason, banned_by, banned_at FROM account_bans WHERE uid=?",
            (uid,),
        ).fetchone()


def is_account_banned(uid: str) -> bool:
    return get_account_ban(uid) is not None


def ban_account(uid: str, reason: str, banned_by: str):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO account_bans (uid, reason, banned_by, banned_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(uid) DO UPDATE SET
                   reason=excluded.reason, banned_by=excluded.banned_by, banned_at=excluded.banned_at""",
            (uid, reason, banned_by, int(time.time())),
        )


def unban_account(uid: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM account_bans WHERE uid=?", (uid,))


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


# ---------- personal data consent ----------

def record_personal_data_consent(uid: str, version: str):
    """Keep the accepted policy version and timestamp for an authenticated user."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO personal_data_consents (uid, version, accepted_at)
               VALUES (?, ?, ?)
               ON CONFLICT(uid) DO UPDATE SET
                 version=excluded.version,
                 accepted_at=excluded.accepted_at""",
            (uid, version, int(time.time())),
        )


def get_personal_data_consent(uid: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT uid, version, accepted_at FROM personal_data_consents WHERE uid=?",
            (uid,),
        ).fetchone()
