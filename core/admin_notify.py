"""
core/admin_notify.py
Notifies every admin in config.ADMIN_IDS the moment a user hosts a bot
(upload or GitHub deploy) — user id, username, file/repo, runtime, size —
so admins can see who's hosting what without digging through the DB.

Sent as a Bot API 10.3 rich message (core/rich_message.py) with colored
in-message buttons back to the Admin Panel / VPS Status. If the rich
call fails for any reason (old Bot API, field mismatch, etc.) this falls
back to a plain Markdown message — same fallback contract every other
rich_message call site in this project follows, so a formatting hiccup
here can never crash the uploader's own deploy flow.
"""

import asyncio
import logging
import config
from core import rich_message as rm

logger = logging.getLogger("bot.admin_notify")


def _plain_text(event: dict) -> str:
    uname = f" (@{event['username']})" if event.get("username") else ""
    lines = [
        "📥 *New bot hosted*",
        f"👤 User ID: `{event['user_id']}`{uname}",
        f"🤖 Bot name: `{event['bot_name']}`",
        f"📦 Source: `{event['source']}`",
        f"⚙️ Runtime: `{event.get('runtime') or '?'}`",
    ]
    if event.get("file_name"):
        lines.append(f"📄 File: `{event['file_name']}`")
    if event.get("repo_url"):
        lines.append(f"🔗 Repo: {event['repo_url']}")
    if event.get("file_size_mb") is not None:
        lines.append(f"💽 Size: {event['file_size_mb']:.2f} MB")
    return "\n".join(lines) + config.CREDIT_FOOTER


def _blocks(event: dict) -> list:
    rows = [
        ["User ID", str(event["user_id"])],
        ["Username", f"@{event['username']}" if event.get("username") else "—"],
        ["Bot name", event["bot_name"]],
        ["Source", event["source"]],
        ["Runtime", event.get("runtime") or "?"],
    ]
    if event.get("file_name"):
        rows.append(["File", event["file_name"]])
    if event.get("repo_url"):
        rows.append(["Repo", event["repo_url"]])
    if event.get("file_size_mb") is not None:
        rows.append(["Size", f"{event['file_size_mb']:.2f} MB"])

    return [
        rm.heading("📥 New Bot Hosted", size=2),
        rm.compact_table(["Field", "Value"], rows),
        rm.button_row([
            {"text": "👑 Admin Panel", "callback_data": "admin:panel", "style": "primary"},
            {"text": "📊 VPS Status", "callback_data": "admin:vps", "style": "primary"},
        ]),
    ]


async def notify_admins(bot, event: dict):
    """
    event keys: user_id, bot_name, source ('upload'|'github'), runtime
    (optional), username (optional), file_name (optional), repo_url
    (optional), file_size_mb (optional).

    Best-effort: a failure notifying one admin (blocked bot, never
    started a DM, etc.) never blocks notifying the rest, and never
    raises back into the caller's deploy flow.
    """
    for admin_id in config.ADMIN_IDS:
        try:
            await asyncio.to_thread(rm.send_blocks, admin_id, _blocks(event))
            continue
        except rm.RichMessageError:
            pass
        except Exception:
            logger.exception("admin_notify: rich send failed for %s", admin_id)

        try:
            await bot.send_message(chat_id=admin_id, text=_plain_text(event), parse_mode="Markdown")
        except Exception:
            logger.warning("admin_notify: could not reach admin %s", admin_id)


# ─────────────────────────────────────────────────────────────
#  MALWARE ALERT — jab koi harmful file upload kare
# ─────────────────────────────────────────────────────────────

def _malware_plain_text(event: dict) -> str:
    uname = f" (@{event['username']})" if event.get("username") else ""
    lines = [
        "🚨 *MALWARE FILE DETECTED & BLOCKED*",
        "",
        f"👤 User ID: `{event['user_id']}`{uname}",
        f"📄 File: `{event['file_name']}`",
        f"💽 Size: {event.get('file_size_mb', 0):.2f} MB",
        f"⚠️ Reason: {event['reason']}",
        "",
        "✅ File block ho gayi — server safe hai",
    ]
    return "\n".join(lines) + config.CREDIT_FOOTER


async def notify_admins_malware(bot, event: dict):
    """
    Jab koi malicious file upload kare toh owner/admins ko alert karo.
    event keys: user_id, username, file_name, file_size_mb, reason
    """
    text = _malware_plain_text(event)
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=text,
                parse_mode="Markdown"
            )
        except Exception:
            logger.warning("malware_notify: could not reach admin %s", admin_id)


# ─────────────────────────────────────────────────────────────
#  STORAGE EMERGENCY — jab disk critical level cross kare
# ─────────────────────────────────────────────────────────────

def _storage_emergency_text(result: dict) -> str:
    disk_line = f"{result['disk_percent']:.0f}%" if result.get("disk_percent") is not None else "n/a"
    lines = [
        "🆘 *Storage critical — emergency cleanup ran*",
        "",
        f"💽 Host disk usage: `{disk_line}`",
        f"📦 App usage: `{result['before_mb']}MB` → `{result['after_mb']}MB`",
        f"🧹 Reclaimed: `{result['reclaimed_mb']}MB`" + (
            f" (+`{result['docker_reclaimed_mb']}MB` from Docker)" if result.get("docker_reclaimed_mb") else ""
        ),
        "",
        "All backup zips were deleted and every bot's log was trimmed hard "
        "to keep the panel from crashing. If usage is still high, delete "
        "an unused bot or raise config.MAX_STORAGE_MB if the host actually "
        "has more room.",
    ]
    return "\n".join(lines) + config.CREDIT_FOOTER


async def notify_admins_storage_emergency(bot, result: dict):
    """Best-effort — same contract as the other notify_* functions here:
    never raises back into the monitor loop that called it."""
    text = _storage_emergency_text(result)
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(chat_id=admin_id, text=text, parse_mode="Markdown")
        except Exception:
            logger.warning("storage_emergency_notify: could not reach admin %s", admin_id)
