"""
core/approval.py
Manual admin-review gate.

core/security.py's automated scan blocks obviously malicious patterns
outright (that flow is unchanged — a blocked file never reaches here).
This module handles everything that DID pass the scan: instead of
deploying it straight away, the code is sent to every admin with
✅ Approve / ❌ Cancel buttons, and nothing runs until an admin taps
Approve. Admins bypass this entirely for their own uploads (see
handlers/upload.py and handlers/github.py) — there's no one above an
admin to approve their code.

A "pending_deploys" row is the record of one upload/clone waiting on a
decision: who it's from, where the code lives on disk, and which chat
+ message to update once it's approved or rejected.
"""

import re
import asyncio
import shutil
import logging
import config
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from database.db import bots_db
from core import deploy, hosting_control

logger = logging.getLogger("bot.approval")


def _esc(value) -> str:
    """Same escaping as handlers/common.py's esc() — duplicated locally
    so core/ doesn't depend on handlers/."""
    return re.sub(r"([_*`\[])", r"\\\1", str(value))


def create_pending(
    owner_id: int,
    owner_username: str | None,
    bot_name: str,
    project_path: str,
    source: str,
    repo_url: str | None,
    file_name: str | None,
    user_chat_id: int,
    user_msg_id: int,
    reservation_id: int | None = None,
) -> int:
    with bots_db() as conn:
        cur = conn.execute(
            """INSERT INTO pending_deploys
               (owner_id, owner_username, bot_name, project_path, source,
                repo_url, file_name, user_chat_id, user_msg_id, reservation_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (owner_id, owner_username, bot_name, project_path, source,
             repo_url, file_name, user_chat_id, user_msg_id, reservation_id),
        )
        return cur.lastrowid


def get_pending(pending_id: int) -> dict | None:
    with bots_db() as conn:
        row = conn.execute(
            "SELECT * FROM pending_deploys WHERE pending_id=?", (pending_id,)
        ).fetchone()
    return dict(row) if row else None


def delete_pending(pending_id: int):
    with bots_db() as conn:
        conn.execute("DELETE FROM pending_deploys WHERE pending_id=?", (pending_id,))


async def send_doc_to_admins(bot, document, caption: str, reply_markup=None, filename: str | None = None) -> int:
    """Sends a document (file_id or open file) to EVERY admin.
    Old code swallowed all errors silently (`except: pass`), so if the
    Markdown caption was rejected or the admin hadn't /start-ed the bot,
    nothing arrived and nobody knew. Now: try Markdown, retry as plain
    text, and log the real reason. Returns how many admins got it."""
    delivered = 0
    for admin_id in config.ADMIN_IDS:
        for parse_mode in ("Markdown", None):
            try:
                if hasattr(document, "seek"):
                    document.seek(0)
                await bot.send_document(
                    chat_id=admin_id, document=document, filename=filename,
                    caption=caption, parse_mode=parse_mode, reply_markup=reply_markup,
                )
                delivered += 1
                break
            except Exception as e:
                logger.error("send_document to admin %s failed (parse_mode=%s): %s", admin_id, parse_mode, e)
    return delivered


def review_caption(p: dict, auto_approved: bool = False) -> str:
    uname = f" (@{p['owner_username']})" if p.get("owner_username") else ""
    lines = [
        "👑 *Premium user bot — auto-approved & running*" if auto_approved
        else "🔎 *New bot — awaiting your approval*",
        f"👤 User ID: `{p['owner_id']}`{uname}",
        f"🤖 Name: `{_esc(p['bot_name'])}`",
        f"📦 Source: `{p['source']}`",
    ]
    if p.get("file_name"):
        lines.append(f"📄 File: `{_esc(p['file_name'])}`")
    if p.get("repo_url"):
        lines.append(f"🔗 Repo: {p['repo_url']}")
    if not auto_approved:
        lines.append("\nCheck the attached code, then tap Approve or Cancel below.")
    return "\n".join(lines) + config.CREDIT_FOOTER


def review_buttons(pending_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"review:approve:{pending_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"review:reject:{pending_id}"),
    ]])


async def approve(bot, pending_id: int) -> tuple[bool, str]:
    """Deploys the pending bot and tells the user. Returns (success, message)
    for whoever tapped the button to see."""
    p = get_pending(pending_id)
    if not p:
        return False, "Already handled (or expired) — nothing to do."

    delete_pending(pending_id)  # consume immediately so a second admin's
                                # tap (or a double-tap) can't deploy twice

    try:
        bot_row = await deploy.deploy_bot_async(p["owner_id"], p["bot_name"], p["project_path"], p["source"], p.get("repo_url"))
    except Exception as e:
        logger.exception("approved deploy failed for pending_id=%s", pending_id)
        shutil.rmtree(p["project_path"], ignore_errors=True)
        try:
            await bot.edit_message_text(
                chat_id=p["user_chat_id"], message_id=p["user_msg_id"],
                text=f"❌ Deploy failed after approval: {e}" + config.CREDIT_FOOTER,
            )
        except Exception:
            pass
        hosting_control.release_slot(p.get("reservation_id"))
        return False, f"Approved, but deploy failed: {e}"

    # The bot now exists in bots.db and counts as a slot by itself — the
    # reservation must be released or every approved bot used 2 slots
    # forever (the panel then reported "slots full" far too early).
    hosting_control.release_slot(p.get("reservation_id"))

    try:
        await bot.edit_message_text(
            chat_id=p["user_chat_id"], message_id=p["user_msg_id"],
            text=(
                f"✅ *{_esc(bot_row['name'])}* approved by admin & is now running!\n"
                f"Runtime: `{bot_row['runtime']}`\n"
                f"Entrypoint: `{bot_row['entrypoint']}`"
                + config.CREDIT_FOOTER
            ),
            parse_mode="Markdown",
        )
    except Exception:
        pass

    return True, f"Approved & deployed \u201c{bot_row['name']}\u201d."


async def reject(bot, pending_id: int) -> tuple[bool, str]:
    """Deletes the pending bot's files and tells the user. Returns
    (success, message) for whoever tapped the button to see."""
    p = get_pending(pending_id)
    if not p:
        return False, "Already handled (or expired) — nothing to do."

    delete_pending(pending_id)
    shutil.rmtree(p["project_path"], ignore_errors=True)
    hosting_control.release_slot(p.get("reservation_id"))

    try:
        await bot.edit_message_text(
            chat_id=p["user_chat_id"], message_id=p["user_msg_id"],
            text=(
                "❌ Your bot was reviewed and rejected by the admin. "
                "It was not deployed and its files were removed."
                + config.CREDIT_FOOTER
            ),
        )
    except Exception:
        pass

    return True, f"Rejected & deleted \u201c{p['bot_name']}\u201d."
