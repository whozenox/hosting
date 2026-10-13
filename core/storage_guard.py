"""
core/storage_guard.py
Keeps the whole panel's disk usage under config.MAX_STORAGE_MB.

Two jobs:
  1. enforce_upload_budget() — called before accepting a new upload/clone,
     rejects it up front if it would push total usage past the limit,
     instead of letting the disk fill up and everything (including the
     panel itself) crash later.
  2. cleanup() — best-effort housekeeping: clears temp/, truncates
     oversized per-bot logs, deletes backup zips older than a few
     minutes (in case a send failed and backup.py's own delete didn't
     run), and prunes dangling Docker images. Run once on startup and
     periodically from core/monitor.py.
"""

import os
import time
import config


class StorageFullError(Exception):
    pass


def _dir_size_bytes(path: str) -> int:
    total = 0
    if not os.path.isdir(path):
        return 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for fname in filenames:
            fp = os.path.join(dirpath, fname)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total


def total_used_mb() -> float:
    """Combined size of everything this panel itself writes to disk —
    uploaded/cloned bot projects, backups, logs, temp downloads. Doesn't
    count Docker images (those are tracked/cleaned separately since they
    live outside BASE_DIR)."""
    dirs = [config.UPLOADS_DIR, config.BACKUPS_DIR, config.LOGS_DIR, config.TEMP_DIR,
            getattr(config, "DEPS_DIR", os.path.join(config.BASE_DIR, "deps"))]
    total = sum(_dir_size_bytes(d) for d in dirs)
    return round(total / 1024 / 1024, 1)


def enforce_upload_budget(incoming_mb: float):
    """Raises StorageFullError if adding `incoming_mb` more data would
    exceed config.MAX_STORAGE_MB. Call this BEFORE saving/extracting a
    new upload or repo clone."""
    used = total_used_mb()
    if used + incoming_mb > config.MAX_STORAGE_MB:
        cleanup()  # try to reclaim space before giving up
        used = total_used_mb()
        if used + incoming_mb > config.MAX_STORAGE_MB:
            raise StorageFullError(
                f"Storage limit reached ({used:.0f}MB / {config.MAX_STORAGE_MB}MB used). "
                f"Delete an old bot or backup first, or ask the admin to raise "
                f"config.MAX_STORAGE_MB."
            )


def _truncate_oversized_logs():
    """Caps every per-bot log file at config.LOG_MAX_KB, keeping only the
    newest bytes (process_manager also checks this before each write, but
    this catches anything that grew before that check existed, or logs
    from bots that no longer run)."""
    limit_bytes = config.LOG_MAX_KB * 1024
    if not os.path.isdir(config.LOGS_DIR):
        return
    for fname in os.listdir(config.LOGS_DIR):
        fp = os.path.join(config.LOGS_DIR, fname)
        try:
            if os.path.isfile(fp) and os.path.getsize(fp) > limit_bytes:
                with open(fp, "rb") as f:
                    f.seek(-limit_bytes, os.SEEK_END)
                    tail = f.read()
                with open(fp, "wb") as f:
                    f.write(tail)
        except OSError:
            pass


def _clear_temp(max_age_seconds: int = 900):
    """Removes leftover temp files. Only files older than max_age_seconds:
    this runs every ~10 min from the monitor, and used to delete EVERY
    file in temp/ — including another user's upload that was still being
    downloaded/extracted at that moment (upload then failed or deployed
    a half-written file). max_age_seconds=0 wipes everything (startup /
    emergency)."""
    if not os.path.isdir(config.TEMP_DIR):
        return
    now = time.time()
    for fname in os.listdir(config.TEMP_DIR):
        if fname == ".gitkeep":
            continue
        fp = os.path.join(config.TEMP_DIR, fname)
        try:
            if os.path.isfile(fp) and (max_age_seconds <= 0 or now - os.path.getmtime(fp) > max_age_seconds):
                os.remove(fp)
        except OSError:
            pass


def _clear_stale_backups(max_age_seconds: int = 600):
    """backup.py deletes its own zip right after sending it — this is
    just a safety net for the rare case that step got skipped (e.g. the
    send failed partway through)."""
    if not os.path.isdir(config.BACKUPS_DIR):
        return
    now = time.time()
    for fname in os.listdir(config.BACKUPS_DIR):
        if fname == ".gitkeep":
            continue
        fp = os.path.join(config.BACKUPS_DIR, fname)
        try:
            if os.path.isfile(fp) and (now - os.path.getmtime(fp)) > max_age_seconds:
                os.remove(fp)
        except OSError:
            pass


def _real_disk_percent() -> float | None:
    """The actual host filesystem's used %, not just this app's own
    folders — a pip/npm cache, apt cache, or something else outside our
    tracked dirs can also fill the disk and crash the bot even while
    total_used_mb() looks fine. Returns None if psutil isn't available."""
    try:
        import psutil
        return psutil.disk_usage(config.BASE_DIR).percent
    except Exception:
        return None


def _delete_all_backups():
    """Emergency version of _clear_stale_backups — deletes every backup
    zip regardless of age. Only runs once usage is already critical, so
    losing a not-yet-downloaded backup is an acceptable trade for not
    crashing the whole panel."""
    if not os.path.isdir(config.BACKUPS_DIR):
        return
    for fname in os.listdir(config.BACKUPS_DIR):
        if fname == ".gitkeep":
            continue
        fp = os.path.join(config.BACKUPS_DIR, fname)
        try:
            if os.path.isfile(fp):
                os.remove(fp)
        except OSError:
            pass


def _truncate_all_logs(limit_kb: int):
    """Like _truncate_oversized_logs, but caps EVERY log file down to
    limit_kb regardless of current size (emergency mode) — normal
    cleanup only trims files that are already over the (larger)
    config.LOG_MAX_KB cap."""
    limit_bytes = limit_kb * 1024
    if not os.path.isdir(config.LOGS_DIR):
        return
    for fname in os.listdir(config.LOGS_DIR):
        fp = os.path.join(config.LOGS_DIR, fname)
        try:
            if os.path.isfile(fp) and os.path.getsize(fp) > limit_bytes:
                with open(fp, "rb") as f:
                    f.seek(-limit_bytes, os.SEEK_END)
                    tail = f.read()
                with open(fp, "wb") as f:
                    f.write(tail)
        except OSError:
            pass


def is_critical() -> bool:
    """True if either our tracked usage or the real host disk is past
    config.EMERGENCY_STORAGE_PERCENT."""
    app_percent = (total_used_mb() / config.MAX_STORAGE_MB) * 100 if config.MAX_STORAGE_MB else 0
    disk_percent = _real_disk_percent()
    threshold = config.EMERGENCY_STORAGE_PERCENT
    return app_percent >= threshold or (disk_percent is not None and disk_percent >= threshold)


def emergency_recover() -> dict:
    """Aggressive cleanup used only when is_critical() is True: deletes
    ALL backups (not just stale ones) and truncates ALL logs down to
    config.EMERGENCY_LOG_MAX_KB, on top of the normal cleanup() steps.
    The idea is to buy back enough disk to keep the whole panel (and
    every hosted bot on it) from crashing when the disk fills up, even
    at the cost of some log history / an undownloaded backup."""
    before = total_used_mb()
    _clear_temp(0)
    _delete_all_backups()
    _truncate_all_logs(config.EMERGENCY_LOG_MAX_KB)

    reclaimed_docker_mb = 0
    if config.USE_DOCKER:
        from core import docker_manager
        reclaimed_docker_mb = docker_manager.prune_dangling_images()

    after = total_used_mb()
    return {
        "before_mb": before,
        "after_mb": after,
        "reclaimed_mb": round(before - after, 1),
        "docker_reclaimed_mb": reclaimed_docker_mb,
        "disk_percent": _real_disk_percent(),
    }


def cleanup() -> dict:
    """Runs all housekeeping steps. Returns a small summary dict, mostly
    useful for an admin command / log line."""
    before = total_used_mb()
    _clear_temp()
    _truncate_oversized_logs()
    _clear_stale_backups()

    reclaimed_docker_mb = 0
    if config.USE_DOCKER:
        from core import docker_manager
        reclaimed_docker_mb = docker_manager.prune_dangling_images()

    after = total_used_mb()
    return {
        "before_mb": before,
        "after_mb": after,
        "reclaimed_mb": round(before - after, 1),
        "docker_reclaimed_mb": reclaimed_docker_mb,
    }
