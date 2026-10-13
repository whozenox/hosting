"""
handlers/account.py
Shows the user's account info: id, plan limits, bot count.
"""

import config
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler
from database.db import bots_db
from core import premium, limits
from handlers.common import safe_edit, esc


async def show_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    is_admin = user.id in config.ADMIN_IDS
    is_prem = premium.is_premium(user.id)

    with bots_db() as conn:
        count = conn.execute("SELECT COUNT(*) c FROM bots WHERE owner_id=?", (user.id,)).fetchone()["c"]

    if is_admin:
        limit = "Unlimited"
    elif is_prem:
        limit = str(config.MAX_BOTS_PER_PREMIUM_USER)
    else:
        limit = str(config.MAX_BOTS_PER_USER)

    role = "Admin" if is_admin else ("Premium 👑" if is_prem else "Free")

    lines = ["👤 *Account*\n", f"User ID: `{user.id}`"]
    if user.username:
        lines.append(f"Username: @{esc(user.username)}")
    lines.append(f"Role: {role}")
    lines.append(f"Hosted bots: {count} / {limit}")
    lines.append(f"CPU limit: {limits.limit_label(user.id, is_admin)}")
    text = "\n".join(lines) + config.CREDIT_FOOTER

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="home:go")]])
    await safe_edit(query, text, reply_markup=kb, parse_mode="Markdown")


def register(app):
    app.add_handler(CallbackQueryHandler(show_account, pattern="^account:show$"))
