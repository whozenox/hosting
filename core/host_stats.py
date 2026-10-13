"""
core/host_stats.py
Whole-VPS resource usage (not per-bot) — CPU, RAM, disk, uptime.
Used by the admin-only /vpsstatus command.
"""

import time
from datetime import datetime, timezone
import psutil
import config


def get_host_stats() -> dict:
    cpu_percent = psutil.cpu_percent(interval=0.5)
    cpu_cores = psutil.cpu_count(logical=True)

    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(config.BASE_DIR)

    boot_time = psutil.boot_time()
    uptime_seconds = int(time.time() - boot_time)

    load = None
    try:
        load = psutil.getloadavg()
    except (AttributeError, OSError):
        pass  # not available on this platform

    return {
        "cpu_percent": cpu_percent,
        "cpu_cores": cpu_cores,
        "load_avg": load,
        "mem_used_mb": round(mem.used / 1024 / 1024, 1),
        "mem_total_mb": round(mem.total / 1024 / 1024, 1),
        "mem_percent": mem.percent,
        "disk_used_gb": round(disk.used / 1024 / 1024 / 1024, 1),
        "disk_total_gb": round(disk.total / 1024 / 1024 / 1024, 1),
        "disk_percent": disk.percent,
        "uptime_seconds": uptime_seconds,
    }


def format_uptime(seconds: int) -> str:
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def bot_uptime_str(started_at: str | None, status: str) -> str:
    """
    Human-readable "how long has this bot been online" string for a
    bots.db row's `started_at` (UTC ISO timestamp, set every time a bot
    transitions to status='running') + `status`.
    Returns e.g. "2d 4h 12m" for a running bot, or "—" if it isn't
    currently running / the timestamp predates this feature.
    """
    if status != "running" or not started_at:
        return "—"
    try:
        started = datetime.fromisoformat(started_at)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        if elapsed < 0:
            elapsed = 0
        return format_uptime(int(elapsed))
    except ValueError:
        return "—"
