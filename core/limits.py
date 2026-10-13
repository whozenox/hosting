"""
core/limits.py
How much CPU each user's hosted bots are allowed to use (Docker mode
only — the process fallback has no cgroup enforcement to hook into).

Every user gets a tier default:
  - free users    -> config.FREE_CPU_LIMIT
  - premium users -> config.PREMIUM_CPU_LIMIT
  - admins        -> config.ADMIN_CPU_LIMIT
An admin can override any individual user's limit (from the Admin Panel
or /setcpu <user_id> <cores>) — the override is stored in users.cpu_limit
and always wins over the tier default until cleared with /resetcpu.
"""

import config
from database.db import users_db
from core import premium


def get_custom_limit(user_id: int) -> float | None:
    """The admin-set override for this user, or None if they're on the
    plain tier default."""
    with users_db() as conn:
        row = conn.execute(
            "SELECT cpu_limit FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
    return row["cpu_limit"] if row and row["cpu_limit"] is not None else None


def set_custom_limit(user_id: int, cores: float | None) -> None:
    """cores=None clears the override, falling back to the tier default."""
    with users_db() as conn:
        conn.execute(
            "INSERT INTO users (user_id, cpu_limit) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET cpu_limit=excluded.cpu_limit",
            (user_id, cores),
        )


def tier_default(user_id: int, is_admin: bool = False) -> float:
    if is_admin:
        return config.ADMIN_CPU_LIMIT
    if premium.is_premium(user_id):
        return config.PREMIUM_CPU_LIMIT
    return config.FREE_CPU_LIMIT


def get_cpu_limit(user_id: int, is_admin: bool = False) -> float:
    """Effective CPU-core limit (e.g. 1.5) to apply to this user's next
    container. A per-user override always wins; otherwise the tier
    default for their current admin/premium/free status."""
    custom = get_custom_limit(user_id)
    if custom is not None:
        return custom
    return tier_default(user_id, is_admin)


def limit_label(user_id: int, is_admin: bool = False) -> str:
    """Human-readable limit + whether it's custom or a tier default, for
    display in the Admin Panel / Account screen."""
    custom = get_custom_limit(user_id)
    if custom is not None:
        return f"{custom:g} cores (custom)"
    return f"{tier_default(user_id, is_admin):g} cores (default)"
