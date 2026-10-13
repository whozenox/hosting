"""
core/monitor.py
Background loop: periodically checks every "running" bot, and if it has
crashed, tries auto_fix once (if the owner allows it) before marking it
crashed and (optionally) notifying the owner.

v2 fixes:
  - No longer performs a restart/write while another bots.db write
    transaction is still open (that caused "database is locked" and
    silently broke auto-restart). Each write is now its own short
    transaction, and the restart happens outside any open transaction.
  - Honors the per-user `notify_on_crash` and `auto_fix_enabled`
    settings that the Settings menu was already writing but nothing read.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
import config
from core import docker_manager, process_manager, deploy, auto_fix, storage_guard, admin_notify, hosting_control, process_guard
from database.db import bots_db, settings_db

logger = logging.getLogger("monitor")

CHECK_INTERVAL_SECONDS = 30

# Run the disk-usage cleanup (temp files, oversized logs, stale backups,
# dangling Docker images) every 20th loop tick (~10 minutes) instead of
# every tick — it's cheap but there's no reason to hammer the filesystem
# every 30 seconds.
_CLEANUP_EVERY_N_TICKS = 20
_tick_count = 0


def _md_escape(text: str) -> str:
    """Escape the legacy-Markdown specials so a bot name can't break parsing."""
    out = str(text)
    for ch in ("_", "*", "`", "["):
        out = out.replace(ch, "\\" + ch)
    return out


def _user_prefs(user_id: int) -> dict:
    """Per-user crash-notify / auto-fix toggles (defaults ON if unset)."""
    with settings_db() as conn:
        r = conn.execute(
            "SELECT notify_on_crash, auto_fix_enabled FROM user_settings WHERE user_id=?",
            (user_id,),
        ).fetchone()
    if r is None:
        return {"notify_on_crash": 1, "auto_fix_enabled": 1}
    return {"notify_on_crash": r["notify_on_crash"], "auto_fix_enabled": r["auto_fix_enabled"]}


async def monitor_loop(bot_app):
    """Run forever as a background task on the PTB application."""
    global _tick_count
    try:
        await asyncio.to_thread(hosting_control.cleanup_stale_reservations, 0)
        await _restore_after_boot(bot_app)
    except Exception:
        logger.exception("boot restore failed")

    while True:
        try:
            await _check_all_bots(bot_app)
        except Exception:
            logger.exception("monitor loop iteration failed")

        # Checked every tick (cheap: just a directory-size scan) because
        # this is the crash-prevention path — waiting for the routine
        # 10-minute cleanup would be too slow once the disk is actually
        # close to full.
        try:
            if await asyncio.to_thread(storage_guard.is_critical):
                result = await asyncio.to_thread(storage_guard.emergency_recover)
                logger.warning("storage EMERGENCY cleanup ran: %s", result)
                await admin_notify.notify_admins_storage_emergency(bot_app.bot, result)
        except Exception:
            logger.exception("storage emergency check failed")

        _tick_count += 1
        if _tick_count % _CLEANUP_EVERY_N_TICKS == 0:
            try:
                freed = await asyncio.to_thread(hosting_control.cleanup_stale_reservations, 30)
                if freed:
                    logger.info("freed %s stale slot reservation(s)", freed)
            except Exception:
                logger.exception("stale reservation cleanup failed")
            try:
                result = await asyncio.to_thread(storage_guard.cleanup)
                logger.info("storage cleanup: %s", result)
            except Exception:
                logger.exception("storage cleanup failed")

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def _restore_after_boot(bot_app):
    """When the panel (re)starts, every hosted process that ran inside the
    same container is gone, but bots.db still says 'running'. Previously
    the monitor then flagged them 'crashed', and after
    AUTO_DELETE_OFFLINE_HOURS deleted them — i.e. every panel restart
    eventually wiped all users' bots. Bring them back up instead."""
    with bots_db() as conn:
        # A deploy that was interrupted by the restart never finished —
        # its half-made row would otherwise hold a hosting slot forever.
        conn.execute("DELETE FROM bots WHERE status='deploying'")
        rows = [dict(r) for r in conn.execute("SELECT * FROM bots WHERE status='running'").fetchall()]

    async def _one(row):
        alive = await asyncio.to_thread(_is_alive, row)
        if alive:
            return
        try:
            await deploy.restart_bot_async(row)
            logger.info("restored bot_id=%s after panel restart", row["bot_id"])
        except Exception as e:
            logger.warning("could not restore bot_id=%s: %s", row["bot_id"], e)
            with bots_db() as conn:
                conn.execute(
                    "UPDATE bots SET status='crashed', started_at=NULL, stopped_at=? WHERE bot_id=?",
                    (datetime.now(timezone.utc).isoformat(), row["bot_id"]),
                )

    if rows:
        logger.info("restoring %s bot(s) that were running before the restart", len(rows))
        await asyncio.gather(*[_one(r) for r in rows], return_exceptions=True)


async def _check_all_bots(bot_app):
    # Automatically delete bots that have been stopped/crashed for the
    # configured retention period. Owner/admin bots are also subject to
    # this only when explicitly stopped/crashed; privileged users bypass
    # hosting *slot* limits, not lifecycle safety.
    if config.AUTO_DELETE_OFFLINE_HOURS > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=config.AUTO_DELETE_OFFLINE_HOURS)).isoformat()
        with bots_db() as conn:
            old_rows = [dict(r) for r in conn.execute(
                "SELECT * FROM bots WHERE status IN ('stopped','crashed') "
                "AND stopped_at IS NOT NULL AND stopped_at <= ?",
                (cutoff,)
            ).fetchall()]
        for old in old_rows:
            try:
                await asyncio.to_thread(deploy.delete_bot, old)
                logger.info("auto-deleted offline bot_id=%s after %sh", old["bot_id"], config.AUTO_DELETE_OFFLINE_HOURS)
                try:
                    await bot_app.bot.send_message(
                        chat_id=old["owner_id"],
                        text=f"🗑 *{_md_escape(old['name'])}* was automatically deleted after being offline for {config.AUTO_DELETE_OFFLINE_HOURS} hour(s).",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
            except Exception:
                logger.exception("auto-delete failed for bot_id=%s", old["bot_id"])

    with bots_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM bots WHERE status='running'"
        ).fetchall()]

    # Each bot is handled independently (one slow auto-fix/rebuild no
    # longer delays crash detection for everybody else's bots).
    results = await asyncio.gather(*[_handle_bot(bot_app, r) for r in rows], return_exceptions=True)
    for r, res in zip(rows, results):
        if isinstance(res, Exception):
            logger.error("monitor failed for bot_id=%s: %r", r["bot_id"], res)


async def _handle_bot(bot_app, row: dict):
    # Fork-bomb guard: process-tree bahut bada => poora tree kill, auto-restart nahi.
    max_procs = getattr(config, "MAX_PROCS_PER_BOT", 40)
    if row.get("pid") and not row.get("container_id"):
        n = await asyncio.to_thread(process_guard.over_limit, row["pid"], max_procs)
        if n:
            logger.warning("bot_id=%s fork-bomb guard: %s processes (limit %s) — killing", row["bot_id"], n, max_procs)
            await asyncio.to_thread(process_manager.stop_process, row["pid"], row.get("entrypoint"), 1.0)
            with bots_db() as conn:
                conn.execute("UPDATE bots SET status='crashed', started_at=NULL, stopped_at=? WHERE bot_id=?",
                             (datetime.now(timezone.utc).isoformat(), row["bot_id"]))
            try:
                await bot_app.bot.send_message(chat_id=row["owner_id"],
                    text=f"⛔ {row['name']} stopped: too many processes ({n}/{max_procs}).")
            except Exception:
                pass
            return
    alive = await asyncio.to_thread(_is_alive, row)
    if alive:
        return

    prefs = _user_prefs(row["owner_id"])
    logger.info("bot_id=%s appears crashed", row["bot_id"])
    logs = await asyncio.to_thread(deploy.get_logs, row, 200)

    # Only attempt auto-fix if the owner has it enabled.
    fixed, message = False, "Bot stopped unexpectedly."
    if prefs["auto_fix_enabled"]:
        fixed, message = await asyncio.to_thread(
            auto_fix.attempt_fix, row["path"], row["runtime"], logs, row["bot_id"]
        )

    restarts = row["restarts"] + 1
    restarted = False

    if fixed and restarts <= config.MAX_AUTO_RESTARTS:
        # Short write first (own transaction), THEN restart outside it.
        with bots_db() as conn:
            conn.execute("UPDATE bots SET restarts=? WHERE bot_id=?", (restarts, row["bot_id"]))
        try:
            await deploy.restart_bot_async(row, True)  # force_rebuild=True: auto_fix just touched files/deps on disk
            restarted = True
        except Exception as e:
            logger.warning("auto-restart failed for bot_id=%s: %s", row["bot_id"], e)
            with bots_db() as conn:
                conn.execute("UPDATE bots SET status='crashed', started_at=NULL, stopped_at=? WHERE bot_id=?", (datetime.now(timezone.utc).isoformat(), row["bot_id"]))
    else:
        with bots_db() as conn:
            conn.execute("UPDATE bots SET status='crashed', started_at=NULL, stopped_at=? WHERE bot_id=?", (datetime.now(timezone.utc).isoformat(), row["bot_id"]))

    if prefs["notify_on_crash"]:
        await _notify_owner(bot_app, row, restarted, message)


def _is_alive(row: dict) -> bool:
    if row.get("container_id"):
        try:
            stats = docker_manager.get_stats(row["container_id"])
            return stats["status"] == "running"
        except Exception:
            return False
    if row.get("pid"):
        if not process_manager.is_running(row["pid"], row.get("entrypoint")):
            return False
        # Sandbox bots: RAM limit enforce (ek bot poore Pterodactyl server ko OOM na kare)
        return process_manager.enforce_memory(row["pid"], row.get("bot_id"))
    return False


async def _notify_owner(bot_app, row: dict, restarted: bool, message: str):
    name = _md_escape(row["name"])
    text = f"⚠️ *{name}* stopped unexpectedly.\n\n"
    text += f"✅ Auto-fix applied & restarted: {message}" if restarted else f"❌ {message}"
    text += config.CREDIT_FOOTER
    try:
        await bot_app.bot.send_message(chat_id=row["owner_id"], text=text, parse_mode="Markdown")
    except Exception:
        logger.warning("could not notify owner %s", row["owner_id"])
