"""
handlers/env.py
Manage per-bot environment variables (stored in settings.db, injected
into the container/process at deploy/restart time).
"""

import config
from core import crypto_store
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler
from database.db import bots_db, settings_db
from handlers.common import safe_edit, esc, btn

AWAITING_ENV_INPUT = "awaiting_env_input"
AWAITING_ENV_BOT_ID = "awaiting_env_bot_id"


async def env_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    buttons = [[InlineKeyboardButton(r["name"], callback_data=f"env:view:{r['bot_id']}")] for r in rows]
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="home:go")])
    await safe_edit(query, "🔑 *Env Vars* — choose a bot:", reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")


async def view_env(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bot_id = int(query.data.split(":")[2])
    with bots_db() as conn:
        row = conn.execute("SELECT * FROM bots WHERE bot_id=? AND owner_id=?", (bot_id, update.effective_user.id)).fetchone()
    if not row:
        await safe_edit(query, "Bot not found.")
        return

    with settings_db() as conn:
        env_rows = conn.execute("SELECT key FROM bot_env WHERE bot_id=?", (bot_id,)).fetchall()

    keys_text = "\n".join(f"• `{r['key']}`" for r in env_rows) or "(none set)"
    text = f"🔑 *{esc(row['name'])}* env vars:\n{keys_text}\n\nSend `KEY=VALUE` to add or update one." + config.CREDIT_FOOTER
    buttons = [
        [btn("➕ Add / Update", callback_data=f"env:add:{bot_id}", style="success")],
        [InlineKeyboardButton("⬅️ Back", callback_data="env:menu")],
    ]
    await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")


async def prompt_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    bot_id = int(query.data.split(":")[2])
    context.user_data[AWAITING_ENV_INPUT] = True
    context.user_data[AWAITING_ENV_BOT_ID] = bot_id
    await safe_edit(query, 
        "Send the variable as `KEY=VALUE`, e.g. `API_TOKEN=abc123`." + config.CREDIT_FOOTER,
        parse_mode="Markdown",
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data is None or not context.user_data.get(AWAITING_ENV_INPUT):
        return
    context.user_data[AWAITING_ENV_INPUT] = False
    bot_id = context.user_data.get(AWAITING_ENV_BOT_ID)

    raw = update.message.text.strip()
    if "=" not in raw:
        await update.message.reply_text("❌ Format must be `KEY=VALUE`. Try again from the Env Vars menu.", parse_mode="Markdown")
        return

    key, value = raw.split("=", 1)
    key = key.strip()
    value = value.strip()

    with bots_db() as conn:
        row = conn.execute("SELECT * FROM bots WHERE bot_id=? AND owner_id=?", (bot_id, update.effective_user.id)).fetchone()
    if not row:
        await update.message.reply_text("Bot not found.")
        return

    with settings_db() as conn:
        conn.execute(
            "INSERT INTO bot_env (bot_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(bot_id, key) DO UPDATE SET value=excluded.value",
            (bot_id, key, crypto_store.encrypt(value)),
        )

    await update.message.reply_text(
        f"✅ Saved `{key}`. Restart the bot from *My Bots* for it to take effect." + config.CREDIT_FOOTER,
        parse_mode="Markdown",
    )


def register(app):
    app.add_handler(CallbackQueryHandler(env_menu, pattern="^env:menu$"))
    app.add_handler(CallbackQueryHandler(view_env, pattern=r"^env:view:\d+$"))
    app.add_handler(CallbackQueryHandler(prompt_add, pattern=r"^env:add:\d+$"))
