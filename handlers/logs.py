"""
handlers/logs.py
Shows the tail of a hosted bot's logs, with an option to trigger auto-fix.
"""

import config
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler
from core import deploy, auto_fix
from database.db import bots_db
from handlers.common import safe_edit, esc, btn


async def logs_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    with bots_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT bot_id, name FROM bots WHERE owner_id=? ORDER BY name", (user_id,)
        ).fetchall()]

    if not rows:
        text = "You don't have any bots yet." + config.CREDIT_FOOTER
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="home:go")]])
        await safe_edit(query, text, reply_markup=kb, parse_mode="Markdown")
        return

    buttons = [[InlineKeyboardButton(r["name"], callback_data=f"logs:view:{r['bot_id']}")] for r in rows]
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="home:go")])
    await safe_edit(query, "📜 *Logs* — choose a bot:", reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")


async def view_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bot_id = int(query.data.split(":")[2])
    with bots_db() as conn:
        row = conn.execute("SELECT * FROM bots WHERE bot_id=? AND owner_id=?", (bot_id, update.effective_user.id)).fetchone()
    if not row:
        await safe_edit(query, "Bot not found.")
        return
    row = dict(row)

    log_text = await asyncio.to_thread(deploy.get_logs, row) or "(no logs yet)"
    snippet = log_text[-3000:].replace("```", "'''")
    buttons = [
        [btn("🔧 Try Auto-Fix", callback_data=f"logs:fix:{bot_id}", style="success")],
        [InlineKeyboardButton("⬅️ Back", callback_data="logs:menu")],
    ]
    await safe_edit(query, 
        f"📜 *{esc(row['name'])}* — last lines:\n```\n{snippet}\n```" + config.CREDIT_FOOTER,
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


async def try_fix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Running auto-fix...")
    bot_id = int(query.data.split(":")[2])
    with bots_db() as conn:
        row = conn.execute("SELECT * FROM bots WHERE bot_id=? AND owner_id=?", (bot_id, update.effective_user.id)).fetchone()
    if not row:
        await safe_edit(query, "Bot not found.")
        return
    row = dict(row)

    log_text = await asyncio.to_thread(deploy.get_logs, row)
    fixed, message = await asyncio.to_thread(auto_fix.attempt_fix, row["path"], row["runtime"], log_text, row["bot_id"])
    if fixed:
        try:
            await deploy.restart_bot_async(row, True)
        except Exception as e:
            message += f"\n(restart failed: {e})"

    prefix = "✅" if fixed else "ℹ️"
    await query.message.reply_text(f"{prefix} {message}" + config.CREDIT_FOOTER, parse_mode="Markdown")


def register(app):
    app.add_handler(CallbackQueryHandler(logs_menu, pattern="^logs:menu$"))
    app.add_handler(CallbackQueryHandler(view_logs, pattern=r"^logs:view:\d+$"))
    app.add_handler(CallbackQueryHandler(try_fix, pattern=r"^logs:fix:\d+$"))
