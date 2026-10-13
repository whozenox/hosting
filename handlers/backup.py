"""
handlers/backup.py
Zips a hosted bot's project folder and sends it back to the user.
"""

import os
import stat
import zipfile
import asyncio
import config
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler
from database.db import bots_db
from handlers.common import safe_edit, esc


def _safe_zip(src_dir: str, zip_path: str) -> str:
    """Zips src_dir WITHOUT following symlinks and without ever leaving it.
    (shutil.make_archive follows file symlinks => a symlink to the panel's
    config.py/database used to end up inside the user's backup.)"""
    root = os.path.realpath(src_dir)
    max_bytes = config.MAX_UPLOAD_MB * 3 * 1024 * 1024
    total = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for cur, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(cur, d))]
            for name in files:
                fp = os.path.join(cur, name)
                try:
                    st = os.lstat(fp)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):      # skips symlinks, fifos, devices
                    continue
                real = os.path.realpath(fp)
                if not real.startswith(root + os.sep):
                    continue
                total += st.st_size
                if total > max_bytes:
                    raise ValueError("Backup too large.")
                zf.write(fp, os.path.relpath(fp, root))
    return zip_path


async def backup_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    buttons = [[InlineKeyboardButton(r["name"], callback_data=f"backup:make:{r['bot_id']}")] for r in rows]
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="home:go")])
    await safe_edit(query, "💾 *Backup* — choose a bot:", reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")


async def make_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Creating backup...")
    bot_id = int(query.data.split(":")[2])
    with bots_db() as conn:
        row = conn.execute("SELECT * FROM bots WHERE bot_id=? AND owner_id=?", (bot_id, update.effective_user.id)).fetchone()
    if not row:
        await safe_edit(query, "Bot not found.")
        return
    row = dict(row)

    os.makedirs(config.BACKUPS_DIR, exist_ok=True)
    archive_path = os.path.join(config.BACKUPS_DIR, f"backup_{bot_id}.zip")
    try:
        await asyncio.to_thread(_safe_zip, row["path"], archive_path)
    except Exception:
        try:
            os.remove(archive_path)
        except OSError:
            pass
        await safe_edit(query, "❌ Backup failed (project too large or unreadable).")
        return

    try:
        with open(archive_path, "rb") as fh:
            await query.message.reply_document(
                document=fh,
                filename=f"{row['name']}_{bot_id}.zip",
                caption=f"💾 Backup of *{esc(row['name'])}*" + config.CREDIT_FOOTER,
                parse_mode="Markdown",
            )
    finally:
        # The zip now lives in the user's Telegram chat — keeping a
        # second copy on disk forever is exactly what was filling up
        # storage. Delete it as soon as it's sent (config.BACKUP_AUTO_DELETE
        # exists only in case someone wants to disable this on a host with
        # disk to spare).
        if config.BACKUP_AUTO_DELETE:
            try:
                os.remove(archive_path)
            except OSError:
                pass


def register(app):
    app.add_handler(CallbackQueryHandler(backup_menu, pattern="^backup:menu$"))
    app.add_handler(CallbackQueryHandler(make_backup, pattern=r"^backup:make:\d+$"))
