"""
handlers/files.py
Simple file browser for a hosted bot's project directory.
"""

import os
import config
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler
from database.db import bots_db
from handlers.common import safe_edit

MAX_INLINE_FILE_BYTES = 60_000  # send as text if smaller than this, else as a document


async def files_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    buttons = [[InlineKeyboardButton(r["name"], callback_data=f"files:browse:{r['bot_id']}:.")] for r in rows]
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="home:go")])
    await safe_edit(query, 
        "📁 *Files* — choose a bot:", reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown"
    )


async def browse(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, _, bot_id_str, rel_path = query.data.split(":", 3)
    bot_id = int(bot_id_str)

    with bots_db() as conn:
        row = conn.execute("SELECT * FROM bots WHERE bot_id=? AND owner_id=?", (bot_id, update.effective_user.id)).fetchone()
    if not row:
        await safe_edit(query, "Bot not found.")
        return

    base = os.path.realpath(row["path"])
    joined = os.path.join(base, rel_path)
    target = os.path.realpath(joined)
    # SECURITY v5.2: real containment check (commonpath, not startswith — "app"
    # used to also match "app2") and never follow a symlink out of the project.
    try:
        inside = os.path.commonpath([base, target]) == base
    except ValueError:
        inside = False
    if not inside or os.path.islink(joined):
        await safe_edit(query, "Invalid path.")
        return

    if os.path.isdir(target):
        entries = sorted(os.listdir(target))
        buttons = []
        for e in entries:
            full = os.path.join(target, e)
            if os.path.islink(full):
                continue
            rel = os.path.relpath(full, base)
            label = f"📁 {e}" if os.path.isdir(full) else f"📄 {e}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"files:browse:{bot_id}:{rel}")])
        parent_rel = os.path.relpath(os.path.dirname(target), base) if target != base else None
        nav = []
        if parent_rel is not None:
            nav.append(InlineKeyboardButton("⬆️ Up", callback_data=f"files:browse:{bot_id}:{parent_rel}"))
        nav.append(InlineKeyboardButton("⬅️ Bots", callback_data="files:menu"))
        buttons.append(nav)
        await safe_edit(query, 
            f"📁 `{rel_path}`", reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown"
        )
    else:
        size = os.path.getsize(target)
        if size <= MAX_INLINE_FILE_BYTES:
            try:
                with open(target, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                await query.message.reply_text(f"```\n{content[:3900]}\n```", parse_mode="Markdown")
            except Exception:
                with open(target, "rb") as fh:
                    await query.message.reply_document(fh, filename=os.path.basename(target))
        else:
            with open(target, "rb") as fh:
                await query.message.reply_document(fh, filename=os.path.basename(target))


def register(app):
    app.add_handler(CallbackQueryHandler(files_menu, pattern="^files:menu$"))
    app.add_handler(CallbackQueryHandler(browse, pattern=r"^files:browse:\d+:.*$"))
