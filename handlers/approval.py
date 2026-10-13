"""
handlers/approval.py
The ✅ Approve / ❌ Cancel buttons admins get on every uploaded/cloned
bot that passed the automated scan (core/security.py) — nothing runs
until an admin reviews the attached code and taps one of these. See
core/approval.py for the actual approve/reject logic.
"""

import config
from telegram import Update
from telegram.ext import ContextTypes, CallbackQueryHandler
from core import approval


async def _finish(query, prefix: str, message: str):
    """Updates the admin's own review message with the outcome, without
    touching the document/caption formatting that might contain
    characters Markdown would choke on."""
    text = f"{prefix} {message}"
    try:
        await query.edit_message_caption(caption=text)
    except Exception:
        try:
            await query.edit_message_text(text)
        except Exception:
            pass


async def handle_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_user.id not in config.ADMIN_IDS:
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer("Deploying...")
    pending_id = int(query.data.split(":")[2])
    ok, message = await approval.approve(context.bot, pending_id)
    await _finish(query, "✅" if ok else "⚠️", message)


async def handle_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_user.id not in config.ADMIN_IDS:
        await query.answer("Not authorized.", show_alert=True)
        return
    await query.answer("Cancelling...")
    pending_id = int(query.data.split(":")[2])
    ok, message = await approval.reject(context.bot, pending_id)
    await _finish(query, "❌" if ok else "⚠️", message)


def register(app):
    app.add_handler(CallbackQueryHandler(handle_approve, pattern=r"^review:approve:\d+$"))
    app.add_handler(CallbackQueryHandler(handle_reject, pattern=r"^review:reject:\d+$"))
