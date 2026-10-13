
"""Hosting moderation: security warnings, bans, and admin controls."""
from datetime import datetime, timezone
import config
from database.db import users_db

def _ensure(user_id, username=None):
    with users_db() as conn:
        conn.execute(
            "INSERT INTO users(user_id,username,is_admin) VALUES(?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=COALESCE(excluded.username,users.username)",
            (user_id, username, 1 if user_id in config.ADMIN_IDS else 0)
        )

def warning_count(user_id):
    with users_db() as conn:
        row=conn.execute("SELECT security_warnings FROM users WHERE user_id=?", (user_id,)).fetchone()
    return int(row["security_warnings"] or 0) if row else 0

def is_banned(user_id):
    if user_id == config.OWNER_ID or user_id in config.ADMIN_IDS:
        return False
    with users_db() as conn:
        row=conn.execute("SELECT is_banned FROM users WHERE user_id=?", (user_id,)).fetchone()
    return bool(row and row["is_banned"])

def record_security_violation(user_id, username=None, reason="Security policy violation"):
    """Increment warning count. On the configured threshold, ban hosting."""
    _ensure(user_id, username)
    if user_id == config.OWNER_ID or user_id in config.ADMIN_IDS:
        return {"warnings": warning_count(user_id), "banned": False, "reason": reason}
    with users_db() as conn:
        row=conn.execute("SELECT security_warnings FROM users WHERE user_id=?", (user_id,)).fetchone()
        n=int(row["security_warnings"] or 0)+1
        banned=1 if n >= config.MAX_SECURITY_WARNINGS else 0
        conn.execute(
            "UPDATE users SET security_warnings=?, is_banned=?, banned_reason=? WHERE user_id=?",
            (n,banned,reason if banned else None,user_id)
        )
    return {"warnings":n,"banned":bool(banned),"reason":reason}

def set_ban(user_id, banned=True, reason="Admin moderation"):
    if user_id == config.OWNER_ID:
        return False, "Owner cannot be banned."
    if user_id in config.ADMIN_IDS and banned:
        return False, "An admin cannot be banned."
    _ensure(user_id)
    with users_db() as conn:
        conn.execute(
            "UPDATE users SET is_banned=?, banned_reason=? WHERE user_id=?",
            (1 if banned else 0, reason if banned else None, user_id)
        )
    return True, "Banned" if banned else "Unbanned"

def list_moderation_users(limit=30):
    with users_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT user_id,username,security_warnings,is_banned,banned_reason "
            "FROM users WHERE security_warnings>0 OR is_banned=1 "
            "ORDER BY is_banned DESC,security_warnings DESC,first_seen DESC LIMIT ?",
            (limit,)
        ).fetchall()]
