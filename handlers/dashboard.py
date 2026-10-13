"""
handlers/dashboard.py
CPU/RAM/status overview across a user's hosted bots.
"""

import asyncio
import config
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler
from core import docker_manager, process_manager
from database.db import bots_db
from handlers.common import safe_edit, esc


def _stats_for(row: dict) -> dict:
    if row.get("container_id"):
        try:
            return docker_manager.get_stats(row["container_id"])
        except Exception:
            return {"status": "unknown", "cpu_percent": 0, "mem_usage_mb": 0}
    if row.get("pid"):
        return process_manager.get_stats(row["pid"], row.get("entrypoint"))
    return {"status": row["status"], "cpu_percent": 0, "mem_usage_mb": 0}


async def show_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    with bots_db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM bots WHERE owner_id=?", (user_id,)).fetchall()]

    if not rows:
        text = "You don't have any bots yet." + config.CREDIT_FOOTER
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="home:go")]])
        await safe_edit(query, text, reply_markup=kb, parse_mode="Markdown")
        return

    # Blocking psutil/docker calls -> worker threads, all bots in parallel.
    all_stats = await asyncio.gather(*[asyncio.to_thread(_stats_for, r) for r in rows])
    lines = ["📊 *Dashboard*\n"]
    for r, stats in zip(rows, all_stats):
        lines.append(
            f"*{esc(r['name'])}* — `{stats.get('status', r['status'])}`\n"
            f"CPU: {stats.get('cpu_percent', 0)}%  |  RAM: {stats.get('mem_usage_mb', 0)}MB\n"
        )

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="home:go")]])
    await safe_edit(query, "\n".join(lines) + config.CREDIT_FOOTER, reply_markup=kb, parse_mode="Markdown")


async def show_bot_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bot_id = int(query.data.split(":")[2])
    with bots_db() as conn:
        row = conn.execute("SELECT * FROM bots WHERE bot_id=? AND owner_id=?", (bot_id, update.effective_user.id)).fetchone()
    if not row:
        await safe_edit(query, "Bot not found.")
        return
    row = dict(row)
    stats = await asyncio.to_thread(_stats_for, row)
    text = (
        f"📊 *{esc(row['name'])}*\n"
        f"Status: `{stats.get('status', row['status'])}`\n"
        f"CPU: {stats.get('cpu_percent', 0)}%\n"
        f"RAM: {stats.get('mem_usage_mb', 0)}MB"
        + config.CREDIT_FOOTER
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data=f"mybots:view:{bot_id}")]])
    await safe_edit(query, text, reply_markup=kb, parse_mode="Markdown")


def register(app):
    app.add_handler(CallbackQueryHandler(show_dashboard, pattern="^dashboard:show$"))
    app.add_handler(CallbackQueryHandler(show_bot_stats, pattern=r"^dashboard:bot:\d+$"))
