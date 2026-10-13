"""
core/premium.py
Grant / revoke / check premium status for users.
Premium can be permanent (premium_until = NULL) or time-limited
(premium_until = ISO timestamp), set by an admin via /addprem.
"""

from datetime import datetime, timedelta, timezone
from database.db import users_db


def ensure_user_row(user_id: int, username: str | None = None, is_admin: bool = False):
    with users_db() as conn:
        conn.execute(
            "INSERT INTO users (user_id, username, is_admin) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
            (user_id, username, 1 if is_admin else 0),
        )


def grant_premium(user_id: int, days: int | None = None):
    """days=None means permanent premium."""
    until = None
    if days is not None:
        until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    with users_db() as conn:
        conn.execute(
            "INSERT INTO users (user_id, is_premium, premium_until) VALUES (?, 1, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET is_premium=1, premium_until=excluded.premium_until",
            (user_id, until),
        )
    return until


def revoke_premium(user_id: int):
    with users_db() as conn:
        conn.execute(
            "UPDATE users SET is_premium=0, premium_until=NULL WHERE user_id=?",
            (user_id,),
        )


def is_premium(user_id: int) -> bool:
    with users_db() as conn:
        row = conn.execute(
            "SELECT is_premium, premium_until FROM users WHERE user_id=?", (user_id,)
        ).fetchone()

    if not row or not row["is_premium"]:
        return False

    until = row["premium_until"]
    if until is None:
        return True  # permanent premium

    try:
        expires_at = datetime.fromisoformat(until)
    except ValueError:
        return True
    # Tolerate rows written by older versions (naive timestamps) by
    # treating a missing tzinfo as UTC, so aware/naive compare can't crash.
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if datetime.now(timezone.utc) >= expires_at:
        # Expired — auto-clean the flag so future checks are fast.
        revoke_premium(user_id)
        return False

    return True


def get_premium_info(user_id: int) -> dict:
    with users_db() as conn:
        row = conn.execute(
            "SELECT is_premium, premium_until FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
    if not row:
        return {"is_premium": False, "premium_until": None}
    return {"is_premium": bool(row["is_premium"]), "premium_until": row["premium_until"]}


def list_premium_users() -> list[dict]:
    with users_db() as conn:
        rows = conn.execute(
            "SELECT user_id, username, premium_until FROM users WHERE is_premium=1"
        ).fetchall()
    return [dict(r) for r in rows]
