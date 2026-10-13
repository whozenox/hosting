"""
bot.py
Main entry point for the Telegram Hosting Bot.
Loads config from config.py (no .env, no os.getenv), registers all
handlers, starts the background monitor, and runs polling.
"""

import asyncio
import logging
import config
from telegram import Update
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import (
    Application, ContextTypes, MessageHandler, CallbackQueryHandler,
    TypeHandler, filters, ApplicationHandlerStop,
)

from handlers import home, upload, github, mybots, files, logs as logs_handler, env, dashboard, backup, settings, account, admin, approval
from core import monitor, force_join, maintenance, storage_guard
from database.db import init_databases

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# httpx logs every request URL at INFO level — and the URL contains the
# bot token. Keep it out of the console / log files (and the spam down).
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

logger = logging.getLogger("bot")

HANDLER_MODULES = [home, upload, github, mybots, files, logs_handler, env, dashboard, backup, settings, account, admin, approval]


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """
    Without this, an exception raised inside a button/command handler is
    only ever printed to the console — the Telegram message never updates
    and the user just sees the button 'do nothing'. This surfaces it.
    """
    err = context.error

    # Harmless noise: stale button taps and Telegram hiccups.
    if isinstance(err, BadRequest) and any(
        t in str(err).lower() for t in ("query is too old", "query id is invalid", "message is not modified")
    ):
        return
    if isinstance(err, (TimedOut, NetworkError)):
        logger.warning("Network problem while handling an update: %s", err)
        return

    # Log a compact summary (the full Update object is huge and noisy).
    summary = "?"
    if isinstance(update, Update):
        u = update.effective_user
        data = update.callback_query.data if update.callback_query else None
        summary = f"user={u.id if u else None} callback={data!r}"
    logger.error("Unhandled exception while processing update (%s)", summary, exc_info=err)

    if isinstance(update, Update) and update.callback_query:
        try:
            await update.callback_query.answer("⚠️ Something went wrong. Please try again.", show_alert=True)
        except Exception:
            pass


async def maintenance_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Registered in group=-2 — runs before EVERYTHING else, including the
    force-join gate. While maintenance mode is on (toggled from the
    Admin Panel), every command/button tap from a non-admin is stopped
    here with a "please wait" message instead of reaching its real
    handler. Admins (config.ADMIN_IDS) always bypass this so they can
    keep testing/fixing the bot while it's "down" for everyone else.
    """
    user = update.effective_user
    if user is None or user.id in config.ADMIN_IDS:
        return
    if not maintenance.is_on():
        return

    text = "🛠 *Bot is under maintenance.*\nPlease wait, we'll be back soon!" + config.CREDIT_FOOTER
    if update.callback_query:
        await update.callback_query.answer("🛠 Bot under maintenance, please wait...", show_alert=True)
    if update.effective_chat:
        await update.effective_chat.send_message(text, parse_mode="Markdown")
    raise ApplicationHandlerStop


async def force_join_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Registered in group=-1 (runs before every other handler). If the user
    hasn't joined every chat in config.FORCE_JOIN_CHATS, this replies with
    the join prompt and raises ApplicationHandlerStop so nothing else in
    the update's normal handler groups runs. Admins always bypass this.
    """
    user = update.effective_user
    if user is None or user.id in config.ADMIN_IDS:
        return

    # Let the "I've Joined" button's own handler do the recheck + message
    # edit — don't gate it here or it could never report back "still not
    # joined" to the user.
    if update.callback_query and update.callback_query.data == "forcejoin:check":
        return

    unjoined = await force_join.get_unjoined_chats(context.bot, user.id)
    if not unjoined:
        return

    text, keyboard = force_join.build_prompt(unjoined)
    if update.callback_query:
        await update.callback_query.answer("Please join first!", show_alert=True)
    if update.effective_chat:
        await update.effective_chat.send_message(text, parse_mode="Markdown", reply_markup=keyboard)
    raise ApplicationHandlerStop


async def force_join_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    unjoined = await force_join.get_unjoined_chats(context.bot, user.id, use_cache=False)
    if unjoined:
        await query.answer("❌ You still haven't joined all of them.", show_alert=True)
        text, keyboard = force_join.build_prompt(unjoined)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)
        return
    await query.answer("✅ Verified, welcome!")
    await query.message.delete()
    await home.start_command(update, context)


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Several flows (GitHub URL entry, env var entry) wait for the user's
    next plain-text message. This router checks user_data flags in
    priority order and dispatches to the right handler.
    """
    if context.user_data.get(github.AWAITING_REPO_URL):
        await github.handle_text(update, context)
        return
    if context.user_data.get(env.AWAITING_ENV_INPUT):
        await env.handle_text(update, context)
        return
    # No active flow waiting on text — ignore silently.


def build_app() -> Application:
    if not config.BOT_TOKEN:
        raise RuntimeError("config.BOT_TOKEN is empty. Set it in config.py before running the bot.")

    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        # THE multi-user fix: by default PTB handles ONE update at a time,
        # so while user A's upload/deploy was running nobody else got any
        # answer. Now updates are processed concurrently.
        .concurrent_updates(getattr(config, "CONCURRENT_UPDATES", 256))
        .connection_pool_size(256)
        .pool_timeout(30)
        .connect_timeout(15)
        .read_timeout(30)
        .write_timeout(60)
        .build()
    )

    # Maintenance gate runs first of all (group=-2) — if maintenance mode
    # is on, non-admins never even reach the force-join gate below.
    app.add_handler(TypeHandler(Update, maintenance_gate), group=-2)

    # Force-join gate runs in group=-1, before every other handler group
    # (home/mybots/etc. all register in the default group=0).
    app.add_handler(TypeHandler(Update, force_join_gate), group=-1)
    app.add_handler(CallbackQueryHandler(force_join_check, pattern="^forcejoin:check$"))

    for module in HANDLER_MODULES:
        module.register(app)

    # Central router for plain-text replies used by multi-step flows.
    # Registered last / low priority group so it only fires when nothing
    # else in the module claimed the update.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router), group=10)
    app.add_error_handler(on_error)

    return app


async def _post_init(app: Application):
    init_databases()
    logger.info("Databases initialized.")
    # Pterodactyl/shared hosts: no root, so bring our own ffmpeg + node.
    from core import dependency_manager as _dm
    for note in await asyncio.to_thread(_dm.ensure_runtime_tools):
        logging.getLogger(__name__).info("runtime tools: %s", note)
    # Docker-free sandbox check (Pterodactyl): result log me dikhta hai
    from core import sandbox as _sb, docker_manager as _dk
    ok, detail = await asyncio.to_thread(_sb.probe)
    logger.info("sandbox: Landlock %s (%s) | Docker %s",
                "READY" if ok else "UNAVAILABLE", detail,
                "ready" if (config.USE_DOCKER and await asyncio.to_thread(_dk.is_docker_available)) else "n/a")
    if getattr(config, "SANDBOX_HARDEN_PANEL", False):
        _sb.harden_panel_process()
    result = await asyncio.to_thread(storage_guard.cleanup)
    logger.info("Startup storage cleanup: %s", result)
    # app.create_task() warns here (the app isn't "running" yet inside
    # post_init and the task would never be awaited) — use the loop directly
    # and keep a reference so it isn't garbage collected.
    app.bot_data["monitor_task"] = asyncio.get_running_loop().create_task(monitor.monitor_loop(app))
    logger.info("Background monitor started.")


async def _post_shutdown(app: Application):
    task = app.bot_data.get("monitor_task")
    if task:
        task.cancel()


def main():
    app = build_app()
    app.post_init = _post_init
    app.post_shutdown = _post_shutdown
    logger.info("Starting bot...")
    # drop_pending_updates: after a restart don't replay a backlog of old
    # button taps (their callback queries are expired anyway).
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
