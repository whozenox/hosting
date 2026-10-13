"""
handlers/upload.py
"Upload Bot" flow: user sends a .zip/.py/.js file, we extract it,
detect the runtime, and deploy it.
"""

import os
import re
import uuid
import shutil
import zipfile
import asyncio
import logging
import config
from telegram.error import BadRequest
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler, MessageHandler, filters
from core import security, deploy, premium, admin_notify, storage_guard, approval, hosting_control, moderation
from datetime import datetime
from database.db import bots_db
from handlers.common import safe_edit, esc

AWAITING_UPLOAD = "awaiting_upload"

logger = logging.getLogger("bot.upload")

# One heavy upload per user at a time (double-tapping / sending two files
# quickly used to race on the same project folder).
_user_locks: dict[int, asyncio.Lock] = {}


def _lock_for(user_id: int) -> asyncio.Lock:
    lock = _user_locks.get(user_id)
    if lock is None:
        lock = _user_locks[user_id] = asyncio.Lock()
    return lock


def _safe_bot_name(file_name: str) -> str:
    base = os.path.splitext(os.path.basename(file_name))[0]
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", base).strip("._")[:40]
    return name or "bot"


def _plain(text: str) -> str:
    """Scanner reasons contain Markdown; strip it for plain-text messages."""
    return str(text).replace("*", "").replace("`", "")


def _extract_sync(local_path: str, project_path: str, file_name: str) -> None:
    """Blocking part of an upload (runs in a worker thread so other users'
    buttons keep working): wipe old folder, extract / copy the files."""
    if os.path.exists(project_path):
        shutil.rmtree(project_path, ignore_errors=True)
    os.makedirs(project_path, exist_ok=True)

    if file_name.lower().endswith(".zip"):
        # Safe extraction: reject traversal, absolute paths, symlinks, and
        # oversized archives; never call extractall() on an untrusted ZIP.
        with zipfile.ZipFile(local_path) as zf:
            root = os.path.realpath(project_path)
            infos = zf.infolist()
            total_uncompressed = sum(i.file_size for i in infos)
            if total_uncompressed > config.MAX_UPLOAD_MB * 5 * 1024 * 1024:
                raise ValueError("ZIP expands beyond the allowed extraction limit.")
            for info in infos:
                name = info.filename.replace("\\\\", "/")
                if name.startswith("/") or any(part == ".." for part in name.split("/")):
                    raise ValueError("ZIP contains an unsafe path.")
                # Unix symlink mode in external attributes.
                mode = (info.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    raise ValueError("ZIP symlinks are not allowed.")
                target = os.path.realpath(os.path.join(root, name))
                if target != root and not target.startswith(root + os.sep):
                    raise ValueError("ZIP path escapes the project directory.")
            for info in infos:
                name = info.filename.replace("\\\\", "/")
                target = os.path.realpath(os.path.join(root, name))
                if info.is_dir():
                    os.makedirs(target, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with zf.open(info, "r") as src, open(target, "xb") as dst:
                    shutil.copyfileobj(src, dst)
        # If the zip contained a single top-level folder, flatten it
        # (ignoring macOS __MACOSX / hidden junk entries).
        entries = [e for e in os.listdir(project_path) if e != "__MACOSX" and not e.startswith(".")]
        if len(entries) == 1 and os.path.isdir(os.path.join(project_path, entries[0])):
            nested = os.path.join(project_path, entries[0])
            for item in os.listdir(nested):
                shutil.move(os.path.join(nested, item), os.path.join(project_path, item))
            os.rmdir(nested)
        shutil.rmtree(os.path.join(project_path, "__MACOSX"), ignore_errors=True)
    else:
        shutil.copy(local_path, os.path.join(project_path, os.path.basename(file_name)))


async def start_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data[AWAITING_UPLOAD] = True
    text = (
        "⬆️ *Upload your bot*\n\n"
        "Send me a `.zip` of your project, or a single `.py`/`.js` file.\n"
        "Make sure it includes `requirements.txt` (Python) or `package.json` (Node) "
        "if it needs extra packages."
        + config.CREDIT_FOOTER
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="home:go")]])
    await safe_edit(query, text, parse_mode="Markdown", reply_markup=kb)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data is None or not context.user_data.get(AWAITING_UPLOAD):
        return  # not in upload flow, ignore

    context.user_data[AWAITING_UPLOAD] = False
    user = update.effective_user
    lock = _lock_for(user.id)
    if lock.locked():
        await update.message.reply_text(
            "⏳ Your previous upload is still being processed. Please wait for it to finish."
        )
        return
    async with lock:
        await _process_upload(update, context)


async def _process_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    doc = update.message.document

    is_admin = user.id in config.ADMIN_IDS
    is_prem = await asyncio.to_thread(premium.is_premium, user.id)

    if await asyncio.to_thread(moderation.is_banned, user.id):
        await update.message.reply_text("⛔ Hosting access is banned for your account." + config.CREDIT_FOOTER)
        return

    file_name = os.path.basename(doc.file_name or "upload.zip")
    ok, reason = security.check_extension(file_name)
    if not ok:
        await update.message.reply_text(f"❌ {reason}" + config.CREDIT_FOOTER, parse_mode="Markdown")
        return

    if doc.file_size and doc.file_size > config.MAX_UPLOAD_MB * 1024 * 1024:
        await update.message.reply_text(
            f"❌ File too large (limit {config.MAX_UPLOAD_MB}MB)." + config.CREDIT_FOOTER
        )
        return

    bot_name = _safe_bot_name(file_name)

    ok, limit_reason = await asyncio.to_thread(hosting_control.check_user_bot_limit, user.id, bot_name, is_prem)
    if not ok:
        await update.message.reply_text(f"❌ {limit_reason}" + config.CREDIT_FOOTER)
        return

    incoming_mb = (doc.file_size / 1024 / 1024) if doc.file_size else config.MAX_UPLOAD_MB
    try:
        await asyncio.to_thread(storage_guard.enforce_upload_budget, incoming_mb)
    except storage_guard.StorageFullError as e:
        await update.message.reply_text(f"❌ {e}" + config.CREDIT_FOOTER)
        return

    status_msg = await update.message.reply_text("⏳ Downloading and deploying...")

    user_dir = os.path.join(config.UPLOADS_DIR, str(user.id))
    os.makedirs(user_dir, exist_ok=True)
    project_path = os.path.join(user_dir, bot_name)

    # Unique temp name: two users uploading "bot.zip" at the same moment
    # used to overwrite / delete each other's file in temp/.
    os.makedirs(config.TEMP_DIR, exist_ok=True)
    local_path = os.path.join(config.TEMP_DIR, f"{user.id}_{uuid.uuid4().hex[:8]}_{file_name}")
    reservation_id = None
    deployed_or_pending = False  # True once project_path is owned by a bot/pending review
    project_dirty = False        # True once we started replacing project_path's contents

    try:
        try:
            tg_file = await doc.get_file()
            try:
                await tg_file.download_to_drive(local_path, read_timeout=120, write_timeout=120, connect_timeout=30)
            except TypeError:  # very old PTB without per-call timeouts
                await tg_file.download_to_drive(local_path)
        except BadRequest as e:
            if "too big" in str(e).lower():
                await status_msg.edit_text(
                    "❌ Telegram doesn't let bots download files bigger than 20MB. "
                    "Please send a smaller zip (without node_modules / venv / .git)."
                )
                return
            raise

        ok, reason = await asyncio.to_thread(security.check_upload_size, local_path)
        if not ok:
            await status_msg.edit_text(f"❌ {reason}")
            return

        # ── ZIP Safety Check (extract se PEHLE) — admins bypass this ──
        if file_name.lower().endswith(".zip") and not is_admin:
            ok, reason = await asyncio.to_thread(security.scan_zip_before_extract, local_path)
            if not ok:
                await status_msg.edit_text(
                    "🚨 *Security Alert!*\n\n"
                    "ZIP file block ho gayi — malicious content mila.\n\n"
                    f"❌ `{file_name}`",
                    parse_mode="Markdown"
                )
                await admin_notify.notify_admins_malware(context.bot, {
                    "user_id":      user.id,
                    "username":     user.username,
                    "file_name":    file_name,
                    "file_size_mb": (doc.file_size / 1024 / 1024) if doc.file_size else 0,
                    "reason":       f"ZIP security: {reason}",
                    "time":         datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                })
                return

        # Same-name bot already hosted? Stop it first (re-upload = update).
        await asyncio.to_thread(deploy.remove_existing, user.id, bot_name)

        project_dirty = True
        try:
            await asyncio.to_thread(_extract_sync, local_path, project_path, file_name)
        except zipfile.BadZipFile:
            await status_msg.edit_text("❌ That file isn't a valid ZIP archive.")
            shutil.rmtree(project_path, ignore_errors=True)
            return

        # Global slot reservation is atomic so simultaneous uploads cannot
        # push the panel above the 50-slot cap. Owner/admin bypass it.
        ok, slot_reason, reservation_id = await asyncio.to_thread(hosting_control.reserve_slot, user.id)
        if not ok:
            await status_msg.edit_text(f"❌ {slot_reason}")
            return

        used, total = await asyncio.to_thread(hosting_control.slot_status)
        if (not is_admin) and used >= config.SLOT_WARNING_AT:
            await status_msg.edit_text(
                f"⚠️ Hosting load is high: `{used}/{total}` slots are occupied.\n"
                "Your upload is being processed; new users may need to wait for a free slot."
                + config.CREDIT_FOOTER,
                parse_mode="Markdown",
            )

        # ── Malware scan — admins ko bhi result dikhao, par block nahi karte ──
        ok, reason = await asyncio.to_thread(security.scan_project_tree, project_path)
        if is_admin and not ok:
            # Admin ko warn karo par deploy karne do
            await status_msg.edit_text(
                f"⚠️ *Admin Override — Scan Alert*\n\n"
                f"Yeh file malicious lag rahi hai:\n`{esc(reason)}`\n\n"
                f"_Admin hone ki wajah se deploy ho raha hai..._" + config.CREDIT_FOOTER,
                parse_mode="Markdown"
            )
            ok = True  # Admin deploy allow
        if not ok:
            violation = await asyncio.to_thread(moderation.record_security_violation, user.id, user.username, reason)

            warning_text = (
                f"\n\n⚠️ Warning `{violation['warnings']}/{config.MAX_SECURITY_WARNINGS}`."
                + ("\n⛔ Hosting access has now been banned after repeated security violations."
                   if violation["banned"] else "")
            )
            await status_msg.edit_text(
                "🚨 *Security Alert!*\n\n"
                "Aapki file block ho gayi kyunki usme malicious/security-sensitive code mila.\n"
                f"❌ `{file_name}`" + warning_text + config.CREDIT_FOOTER,
                parse_mode="Markdown"
            )

            # ── Owner/Admins ko alert karo ────────────────────────
            await admin_notify.notify_admins_malware(context.bot, {
                "user_id":      user.id,
                "username":     user.username,
                "file_name":    file_name,
                "file_size_mb": (doc.file_size / 1024 / 1024) if doc.file_size else 0,
                "reason":       reason,
                "time":         datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            return

        approval_required = await asyncio.to_thread(hosting_control.get_approval_required)
        if is_admin or is_prem or not approval_required:
            # Admins/Premium skip manual approval; when approval is OFF,
            # all users who pass the automated scan deploy directly.
            await status_msg.edit_text("⏳ Installing dependencies & starting your bot...")
            try:
                bot_row = await deploy.deploy_bot_async(user.id, bot_name, project_path, "upload", None, True)
            except deploy.DeployError as e:
                await status_msg.edit_text(f"❌ Deploy failed: {str(e)[-3500:]}")
                return
            deployed_or_pending = True
            text = (
                f"✅ *{esc(bot_row['name'])}* deployed and running!\n"
                f"Runtime: `{bot_row['runtime']}`\n"
                f"Entrypoint: `{bot_row['entrypoint']}`"
                + config.CREDIT_FOOTER
            )
            await status_msg.edit_text(text, parse_mode="Markdown")
            await admin_notify.notify_admins(context.bot, {
                "user_id": user.id,
                "username": user.username,
                "bot_name": bot_row["name"],
                "source": "upload",
                "runtime": bot_row["runtime"],
                "file_name": file_name,
                "file_size_mb": (doc.file_size / 1024 / 1024) if doc.file_size else None,
            })
            # Admin always receives a copy of a user-hosted file.
            if not is_admin:
                await approval.send_doc_to_admins(
                    context.bot, doc.file_id,
                    approval.review_caption({
                        "owner_id": user.id, "owner_username": user.username,
                        "bot_name": bot_name, "source": "upload",
                        "file_name": file_name, "repo_url": None,
                    }, auto_approved=True),
                )
            return

        # ── Manual admin review — nothing runs until an admin taps ✅ Approve ──
        await status_msg.edit_text(
            f"⏳ *{esc(bot_name)}* uploaded — sent to the admin for review.\n"
            "You'll get a message here once it's approved (and running) or rejected."
            + config.CREDIT_FOOTER,
            parse_mode="Markdown",
        )

        pending_id = await asyncio.to_thread(
            approval.create_pending,
            owner_id=user.id,
            owner_username=user.username,
            bot_name=bot_name,
            project_path=project_path,
            source="upload",
            repo_url=None,
            file_name=file_name,
            user_chat_id=status_msg.chat_id,
            user_msg_id=status_msg.message_id,
            reservation_id=reservation_id,
        )
        deployed_or_pending = True
        reservation_id = None  # now owned by the pending row

        caption = approval.review_caption({
            "owner_id": user.id,
            "owner_username": user.username,
            "bot_name": bot_name,
            "source": "upload",
            "file_name": file_name,
            "repo_url": None,
        })
        delivered = await approval.send_doc_to_admins(
            context.bot, doc.file_id, caption, reply_markup=approval.review_buttons(pending_id)
        )
        if not delivered:
            await status_msg.edit_text(
                "⚠️ Admin ko file nahi pahunch saki (admin ne bot /start nahi kiya ya ADMIN_IDS galat hai). "
                "Owner se contact karein." + config.CREDIT_FOOTER
            )

    except Exception as e:
        logger.exception("upload failed for user %s", user.id)
        try:
            await status_msg.edit_text(f"❌ Something went wrong while processing your upload: {str(e)[:300]}")
        except Exception:
            pass
    finally:
        # Whatever happened, never leak the reservation, temp file or a
        # half-made project folder.
        if reservation_id:
            await asyncio.to_thread(hosting_control.release_slot, reservation_id)
        if project_dirty and not deployed_or_pending:
            await asyncio.to_thread(shutil.rmtree, project_path, True)
        try:
            os.remove(local_path)
        except OSError:
            pass


def register(app):
    app.add_handler(CallbackQueryHandler(start_upload, pattern="^upload:start$"))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
