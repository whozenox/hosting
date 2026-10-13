"""
handlers/github.py
"Deploy from GitHub" flow: user sends a repo URL, we clone + deploy it.
"""

import os
import shutil
import asyncio
import logging
import config
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CallbackQueryHandler, MessageHandler, filters
from datetime import datetime
from core import github_manager, security, deploy, premium, admin_notify, storage_guard, approval, hosting_control, moderation
from database.db import bots_db
from handlers.common import safe_edit, esc

AWAITING_REPO_URL = "awaiting_repo_url"

logger = logging.getLogger("bot.github")
_user_locks: dict[int, asyncio.Lock] = {}


def _lock_for(user_id: int) -> asyncio.Lock:
    lock = _user_locks.get(user_id)
    if lock is None:
        lock = _user_locks[user_id] = asyncio.Lock()
    return lock


def _plain(text: str) -> str:
    return str(text).replace("*", "").replace("`", "")


async def _send_review_zip(bot, project_path: str, tag: str, caption: str, reply_markup=None) -> int:
    """Zips the project (in a thread) and sends it to every admin."""
    zbase = os.path.join(config.TEMP_DIR, tag)
    zpath = await asyncio.to_thread(shutil.make_archive, zbase, "zip", project_path)
    try:
        with open(zpath, "rb") as fh:
            return await approval.send_doc_to_admins(
                bot, fh, caption, reply_markup=reply_markup, filename=os.path.basename(zpath),
            )
    finally:
        try:
            os.remove(zpath)
        except OSError:
            pass


async def start_github(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data[AWAITING_REPO_URL] = True
    text = (
        "🐙 *Deploy from GitHub*\n\n"
        "Send me a public repo URL, e.g.\n`https://github.com/user/my-bot`"
        + config.CREDIT_FOOTER
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="home:go")]])
    await safe_edit(query, text, parse_mode="Markdown", reply_markup=kb)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data is None or not context.user_data.get(AWAITING_REPO_URL):
        return
    context.user_data[AWAITING_REPO_URL] = False

    user = update.effective_user
    lock = _lock_for(user.id)
    if lock.locked():
        await update.message.reply_text("⏳ Your previous deploy is still running. Please wait for it to finish.")
        return
    async with lock:
        await _process_repo(update, context)


async def _process_repo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    repo_url = update.message.text.strip()

    if not github_manager.is_valid_github_url(repo_url):
        await update.message.reply_text(
            "❌ That doesn't look like a valid GitHub repo URL. Try again with /start."
        )
        return

    is_admin = user.id in config.ADMIN_IDS
    is_prem = await asyncio.to_thread(premium.is_premium, user.id)

    if await asyncio.to_thread(moderation.is_banned, user.id):
        await update.message.reply_text("⛔ Hosting access is banned for your account." + config.CREDIT_FOOTER)
        return

    bot_name = repo_url.rstrip("/").split("/")[-1].replace(".git", "")[:40]

    ok, limit_reason = await asyncio.to_thread(hosting_control.check_user_bot_limit, user.id, bot_name, is_prem)
    if not ok:
        await update.message.reply_text(f"❌ {limit_reason}" + config.CREDIT_FOOTER)
        return

    try:
        await asyncio.to_thread(storage_guard.enforce_upload_budget, 0)
    except storage_guard.StorageFullError as e:
        await update.message.reply_text(f"❌ {e}")
        return

    ok, slot_reason, reservation_id = await asyncio.to_thread(hosting_control.reserve_slot, user.id)
    if not ok:
        await update.message.reply_text(f"❌ {slot_reason}")
        return

    status_msg = None
    project_path = None
    deployed_or_pending = False

    try:
        used, total = await asyncio.to_thread(hosting_control.slot_status)
        if not is_admin and used >= config.SLOT_WARNING_AT:
            await update.message.reply_text(
                f"⚠️ Hosting load is high: `{used}/{total}` slots are occupied. New users may need to wait.",
                parse_mode="Markdown",
            )

        status_msg = await update.message.reply_text("⏳ Cloning repository...")

        # Re-deploying the same repo: stop the old copy first.
        await asyncio.to_thread(deploy.remove_existing, user.id, bot_name)

        ok, result = await asyncio.wait_for(
            asyncio.to_thread(github_manager.clone_repo, repo_url, user.id, bot_name), timeout=300
        )
        if not ok:
            await status_msg.edit_text(f"❌ {_plain(result)[:3500]}")
            return

        project_path = result

        # We can't know a repo's size before cloning it — check afterward and
        # bail out if it pushed us over budget (this deletes the just-cloned
        # copy, not any already-deployed bot).
        try:
            await asyncio.to_thread(storage_guard.enforce_upload_budget, 0)
        except storage_guard.StorageFullError as e:
            await status_msg.edit_text(f"❌ Repo too large for available storage. {e}")
            return

        if is_admin:
            ok, reason = True, ""
        else:
            ok, reason = await asyncio.to_thread(security.scan_project_tree, project_path)
        if not ok:
            violation = await asyncio.to_thread(moderation.record_security_violation, user.id, user.username, reason)
            warning = f" Warning {violation['warnings']}/{config.MAX_SECURITY_WARNINGS}."
            if violation["banned"]:
                warning += " Hosting access is now banned after repeated security violations."
            await status_msg.edit_text(f"❌ {_plain(reason)[:3000]}\n\n⚠️{warning}" + config.CREDIT_FOOTER)
            await admin_notify.notify_admins_malware(context.bot, {
                "user_id": user.id, "username": user.username,
                "file_name": None, "file_size_mb": 0, "reason": reason,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            return

        approval_required = await asyncio.to_thread(hosting_control.get_approval_required)
        if is_admin or is_prem or not approval_required:
            # Admins & Premium users skip manual approval.
            await status_msg.edit_text("⏳ Installing dependencies & starting your bot...")
            try:
                bot_row = await deploy.deploy_bot_async(user.id, bot_name, project_path, "github", repo_url, True)
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
                "source": "github",
                "runtime": bot_row["runtime"],
                "repo_url": repo_url,
            })
            # Send a review copy to admins even when approval is OFF
            # (one copy — this used to send the same zip twice for premium users).
            if not is_admin:
                await _send_review_zip(
                    context.bot, project_path, f"hosted_{user.id}_{bot_name}",
                    approval.review_caption({
                        "owner_id": user.id, "owner_username": user.username,
                        "bot_name": bot_name, "source": "github",
                        "file_name": None, "repo_url": repo_url,
                    }, auto_approved=True),
                )
            return

        # ── Manual admin review — nothing runs until an admin taps ✅ Approve ──
        await status_msg.edit_text(
            f"⏳ *{esc(bot_name)}* cloned — sent to the admin for review.\n"
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
            source="github",
            repo_url=repo_url,
            file_name=None,
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
            "source": "github",
            "file_name": None,
            "repo_url": repo_url,
        })
        delivered = await _send_review_zip(
            context.bot, project_path, f"review_{pending_id}", caption,
            reply_markup=approval.review_buttons(pending_id),
        )
        if not delivered:
            await status_msg.edit_text(
                "⚠️ Admin ko file nahi pahunch saki. Owner se contact karein." + config.CREDIT_FOOTER
            )

    except asyncio.TimeoutError:
        if status_msg:
            await status_msg.edit_text("❌ Cloning took too long (5 min) and was cancelled. Try a smaller repo.")
    except Exception as e:
        logger.exception("github deploy failed for user %s", user.id)
        try:
            if status_msg:
                await status_msg.edit_text(f"❌ Something went wrong: {str(e)[:300]}")
            else:
                await update.message.reply_text(f"❌ Something went wrong: {str(e)[:300]}")
        except Exception:
            pass
    finally:
        # Never leak the slot reservation or a half-made project folder.
        if reservation_id:
            await asyncio.to_thread(hosting_control.release_slot, reservation_id)
        if project_path and not deployed_or_pending:
            await asyncio.to_thread(shutil.rmtree, project_path, True)


def register(app):
    app.add_handler(CallbackQueryHandler(start_github, pattern="^github:start$"))
    # NOTE: handle_text is dispatched centrally from bot.py's text_router,
    # not registered directly here, since several flows share plain-text input.
