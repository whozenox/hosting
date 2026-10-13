"""
database/db.py
SQLite helpers for users.db, bots.db and settings.db.
The .db files themselves are created automatically on first run —
they are not checked into the repo.
"""

import os
import sqlite3
import contextlib
import config

_initializing = False


def _open(path: str) -> sqlite3.Connection:
    # timeout=30: many users + background monitor write at the same time;
    # the default 5s made "database is locked" errors under load.
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        # WAL lets readers and a writer work simultaneously.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    except sqlite3.DatabaseError:
        pass
    return conn


def _connect(path: str) -> sqlite3.Connection:
    # Self-heal: if a .db file was deleted/replaced while the bot is
    # running (host file manager, re-upload, wipe), sqlite silently
    # creates a brand-new EMPTY file and every query then fails with
    # "no such table". Detect that and rebuild the tables right away.
    missing = (not os.path.exists(path)) or os.path.getsize(path) == 0
    conn = _open(path)
    if missing:
        global _initializing
        if not _initializing:
            _initializing = True
            try:
                conn.close()
                init_databases()
            finally:
                _initializing = False
            conn = _open(path)
    return conn


@contextlib.contextmanager
def users_db():
    conn = _connect(config.USERS_DB)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@contextlib.contextmanager
def bots_db():
    conn = _connect(config.BOTS_DB)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@contextlib.contextmanager
def settings_db():
    conn = _connect(config.SETTINGS_DB)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _add_column_if_missing(conn, table: str, column: str, coltype: str):
    """Adds a column to an existing table if it isn't there yet.
    Safe to call every startup — needed so bots.db/users.db created by an
    older version of the bot pick up new columns without wiping data."""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def init_databases():
    """Create all tables if they don't already exist. Call once on startup."""
    import os
    os.makedirs(config.DATABASE_DIR, exist_ok=True)
    os.makedirs(config.UPLOADS_DIR, exist_ok=True)
    os.makedirs(config.CONTAINERS_DIR, exist_ok=True)
    os.makedirs(config.BACKUPS_DIR, exist_ok=True)
    os.makedirs(config.LOGS_DIR, exist_ok=True)
    os.makedirs(config.TEMP_DIR, exist_ok=True)

    with users_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id      INTEGER PRIMARY KEY,
                username     TEXT,
                first_seen   TEXT DEFAULT CURRENT_TIMESTAMP,
                is_admin     INTEGER DEFAULT 0,
                is_banned    INTEGER DEFAULT 0,
                is_premium   INTEGER DEFAULT 0,
                premium_until TEXT,
                cpu_limit    REAL,
                security_warnings INTEGER DEFAULT 0,
                banned_reason TEXT
            )
        """)
        _add_column_if_missing(conn, "users", "is_premium", "INTEGER DEFAULT 0")
        _add_column_if_missing(conn, "users", "premium_until", "TEXT")
        _add_column_if_missing(conn, "users", "cpu_limit", "REAL")
        _add_column_if_missing(conn, "users", "security_warnings", "INTEGER DEFAULT 0")
        _add_column_if_missing(conn, "users", "banned_reason", "TEXT")

    with bots_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bots (
                bot_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id     INTEGER NOT NULL,
                name         TEXT NOT NULL,
                source       TEXT DEFAULT 'upload',   -- upload | github
                repo_url     TEXT,
                runtime      TEXT,                    -- python | node
                entrypoint   TEXT,
                path         TEXT NOT NULL,
                container_id TEXT,
                pid          INTEGER,
                status       TEXT DEFAULT 'stopped',   -- running | stopped | crashed
                restarts     INTEGER DEFAULT 0,
                created_at   TEXT DEFAULT CURRENT_TIMESTAMP,
                started_at   TEXT,                     -- UTC ISO timestamp of the last time status became 'running'
                UNIQUE(owner_id, name)
            )
        """)
        _add_column_if_missing(conn, "bots", "started_at", "TEXT")
        # Tracks the Docker image tag currently backing this bot (Docker
        # mode only), so a rebuild or delete can remove the OLD image
        # instead of leaving it on disk forever. NULL in process-fallback
        # mode (config.USE_DOCKER = False).
        _add_column_if_missing(conn, "bots", "image_tag", "TEXT")
        _add_column_if_missing(conn, "bots", "stopped_at", "TEXT")
        _add_column_if_missing(conn, "bots", "last_owner_notify_at", "TEXT")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_deploys (
                pending_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id       INTEGER NOT NULL,
                owner_username TEXT,
                bot_name       TEXT NOT NULL,
                project_path   TEXT NOT NULL,
                source         TEXT DEFAULT 'upload',   -- upload | github
                repo_url       TEXT,
                file_name      TEXT,
                user_chat_id   INTEGER NOT NULL,
                user_msg_id    INTEGER NOT NULL,
                created_at     TEXT DEFAULT CURRENT_TIMESTAMP,
                reservation_id INTEGER
            )
        """)

        _add_column_if_missing(conn, "pending_deploys", "reservation_id", "INTEGER")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS slot_reservations (
                reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
    with settings_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_env (
                bot_id  INTEGER NOT NULL,
                key     TEXT NOT NULL,
                value   TEXT,
                PRIMARY KEY (bot_id, key)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id  INTEGER PRIMARY KEY,
                notify_on_crash INTEGER DEFAULT 1,
                auto_fix_enabled INTEGER DEFAULT 1
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS system_settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
