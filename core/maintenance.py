"""
core/maintenance.py
Global maintenance-mode toggle for the whole bot.

When ON, every update from a non-admin user is intercepted (see
bot.py's maintenance_gate, registered in group=-2 — before even the
force-join gate) with a "under maintenance, please wait" message,
before it ever reaches a real handler. Admins (config.ADMIN_IDS)
always bypass it, so whoever's fixing things can keep working while
the bot is "down" for everyone else.

Persisted as a single row in settings.db so the toggle survives a
bot restart.
"""

from database.db import settings_db

_KEY = "maintenance_mode"


def is_on() -> bool:
    with settings_db() as conn:
        row = conn.execute(
            "SELECT value FROM system_settings WHERE key=?", (_KEY,)
        ).fetchone()
    return bool(row) and row["value"] == "1"


def set_on(value: bool) -> None:
    with settings_db() as conn:
        conn.execute(
            "INSERT INTO system_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_KEY, "1" if value else "0"),
        )


def toggle() -> bool:
    """Flips the current state and returns the new state."""
    new_state = not is_on()
    set_on(new_state)
    return new_state
