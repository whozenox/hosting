
"""Capacity, approval, and offline-retention helpers for the hosting panel."""
from datetime import datetime, timezone
import config
from database.db import bots_db, users_db, settings_db

def _now():
    return datetime.now(timezone.utc).isoformat()

def is_privileged(user_id: int) -> bool:
    return user_id == config.OWNER_ID or user_id in config.ADMIN_IDS

def is_banned(user_id: int) -> bool:
    if is_privileged(user_id):
        return False
    with users_db() as conn:
        row = conn.execute("SELECT is_banned FROM users WHERE user_id=?", (user_id,)).fetchone()
    return bool(row and row["is_banned"])

def active_slot_count() -> int:
    with bots_db() as conn:
        bots = conn.execute(
            "SELECT COUNT(*) c FROM bots WHERE status IN ('running','deploying','stopped','crashed')"
        ).fetchone()["c"]
        reserved = conn.execute("SELECT COUNT(*) c FROM slot_reservations").fetchone()["c"]
        pending_unreserved = conn.execute(
            "SELECT COUNT(*) c FROM pending_deploys WHERE reservation_id IS NULL"
        ).fetchone()["c"]
    return bots + reserved + pending_unreserved

def _ensure_user(conn, user_id):
    conn.execute(
        "INSERT INTO users (user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING",
        (user_id,)
    )

def reserve_slot(user_id: int):
    """Reserve one global slot. Returns (ok, message, reservation_id).
    Privileged users bypass the global cap and don't create reservations.
    """
    if is_privileged(user_id):
        return True, "", None
    if is_banned(user_id):
        return False, "⛔ Your hosting access is banned by an administrator.", None

    # BEGIN IMMEDIATE serializes concurrent slot checks.
    with bots_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        bots = conn.execute(
            "SELECT COUNT(*) c FROM bots WHERE status IN ('running','deploying','stopped','crashed')"
        ).fetchone()["c"]
        reserved = conn.execute("SELECT COUNT(*) c FROM slot_reservations").fetchone()["c"]
        pending_unreserved = conn.execute(
            "SELECT COUNT(*) c FROM pending_deploys WHERE reservation_id IS NULL"
        ).fetchone()["c"]
        used = bots + reserved + pending_unreserved
        if used >= config.MAX_HOSTING_SLOTS:
            return False, (
                f"⚠️ Hosting slots full: `{used}/{config.MAX_HOSTING_SLOTS}`.\n"
                "Better next time 🥲 Please wait for a slot to become available."
            ), None
        cur = conn.execute(
            "INSERT INTO slot_reservations(owner_id, created_at) VALUES (?, ?)",
            (user_id, _now())
        )
        rid = cur.lastrowid
    return True, "", rid

def release_slot(reservation_id):
    if not reservation_id:
        return
    with bots_db() as conn:
        conn.execute("DELETE FROM slot_reservations WHERE reservation_id=?", (reservation_id,))

def attach_reservation_to_pending(reservation_id, pending_id):
    if not reservation_id or not pending_id:
        return
    with bots_db() as conn:
        conn.execute(
            "UPDATE pending_deploys SET reservation_id=? WHERE pending_id=?",
            (reservation_id, pending_id)
        )

def slot_status():
    used = active_slot_count()
    return used, config.MAX_HOSTING_SLOTS

def approval_required() -> bool:
    return bool(config.APPROVAL_REQUIRED)

def set_approval_required(enabled: bool):
    with settings_db() as conn:
        conn.execute(
            "INSERT INTO system_settings(key,value) VALUES('approval_required',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("1" if enabled else "0",)
        )

def get_approval_required() -> bool:
    with settings_db() as conn:
        row = conn.execute(
            "SELECT value FROM system_settings WHERE key='approval_required'"
        ).fetchone()
    return bool(int(row["value"])) if row and str(row["value"]).isdigit() else approval_required()

def offline_since(row):
    # started_at is NULL after a stop/crash; stopped_at records when that
    # non-running period began.
    return row.get("stopped_at")


def check_user_bot_limit(user_id: int, bot_name: str, is_premium: bool = False):
    """Enforces config.MAX_BOTS_PER_USER / MAX_BOTS_PER_PREMIUM_USER (they
    were shown on the Account screen but never actually checked).
    Re-deploying a bot under a name the user already has doesn't count as
    an extra bot. Returns (ok, message)."""
    if is_privileged(user_id):
        return True, ""
    limit = config.MAX_BOTS_PER_PREMIUM_USER if is_premium else config.MAX_BOTS_PER_USER
    with bots_db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) c FROM bots WHERE owner_id=? AND name<>?", (user_id, bot_name)
        ).fetchone()["c"]
        pending = conn.execute(
            "SELECT COUNT(*) c FROM pending_deploys WHERE owner_id=? AND bot_name<>?", (user_id, bot_name)
        ).fetchone()["c"]
    if count + pending >= limit:
        tier = "premium" if is_premium else "free"
        return False, f"You've reached your {tier} bot limit ({limit}). Delete one of your bots first."
    return True, ""


def cleanup_stale_reservations(max_age_minutes: int = 30) -> int:
    """Frees slot reservations that no longer belong to anything: a
    crash / restart / unexpected error in the middle of an upload used to
    leave its reservation behind forever, so the 50 slots slowly filled
    with ghosts and everyone got 'Hosting slots full'. Reservations that
    are attached to a pending admin review are never touched.
    max_age_minutes=0 clears every unattached reservation (used at boot)."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)).isoformat()
    with bots_db() as conn:
        cur = conn.execute(
            "DELETE FROM slot_reservations WHERE created_at <= ? AND reservation_id NOT IN "
            "(SELECT reservation_id FROM pending_deploys WHERE reservation_id IS NOT NULL)",
            (cutoff,),
        )
        return cur.rowcount
