"""
handlers/settings.py
Per-user preferences: crash notifications, auto-fix toggle.
"""

import config
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler
from database.db import settings_db
from handlers.common import safe_edit, btn


def _get_settings(user_id: int) -> dict:
    with settings_db() as conn:
        row = conn.execute("SELECT * FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO user_settings (user_id) VALUES (?)", (user_id,))
            row = conn.execute("SELECT * FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
        return dict(row)


async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    s = _get_settings(user_id)

    notify_label = "🔔 Crash Notifications: ON" if s["notify_on_crash"] else "🔕 Crash Notifications: OFF"
    autofix_label = "🔧 Auto-Fix: ON" if s["auto_fix_enabled"] else "🔧 Auto-Fix: OFF"
    notify_style = "success" if s["notify_on_crash"] else "danger"
    autofix_style = "success" if s["auto_fix_enabled"] else "danger"

    buttons = [
        [btn(notify_label, callback_data="settings:toggle_notify", style=notify_style)],
        [btn(autofix_label, callback_data="settings:toggle_autofix", style=autofix_style)],
        [InlineKeyboardButton("⬅️ Back", callback_data="home:go")],
    ]
    text = "⚙️ *Settings*" + config.CREDIT_FOOTER
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")


async def toggle_notify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    s = _get_settings(user_id)
    new_val = 0 if s["notify_on_crash"] else 1
    with settings_db() as conn:
        conn.execute("UPDATE user_settings SET notify_on_crash=? WHERE user_id=?", (new_val, user_id))
    await settings_menu(update, context)


async def toggle_autofix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    s = _get_settings(user_id)
    new_val = 0 if s["auto_fix_enabled"] else 1
    with settings_db() as conn:
        conn.execute("UPDATE user_settings SET auto_fix_enabled=? WHERE user_id=?", (new_val, user_id))
    await settings_menu(update, context)


def register(app):
    app.add_handler(CallbackQueryHandler(settings_menu, pattern="^settings:menu$"))
    app.add_handler(CallbackQueryHandler(toggle_notify, pattern="^settings:toggle_notify$"))
    app.add_handler(CallbackQueryHandler(toggle_autofix, pattern="^settings:toggle_autofix$"))
