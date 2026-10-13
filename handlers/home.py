"""
handlers/home.py
/start command and the main inline menu shared by every other handler.

v2: the main screen is now sent as a Bot API 10.3 rich message (video +
heading + a compact info table + buttons embedded in the message itself)
— same treatment as the Node.js sibling bot's start screen. Every rich
call has a plain-PTB fallback so a Bot API field mismatch never leaves
the user without a working menu.
"""

import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler
import config
from core import rich_message as rm
from core import docker_manager, sandbox
from database.db import users_db, bots_db
from handlers.common import safe_edit, esc, btn


# Single source of truth for the menu layout — used to build BOTH the
# rich in-message buttons and the plain InlineKeyboardMarkup fallback,
# so the two never drift out of sync.
def _menu_rows(user_id: int | None = None) -> list[list[dict]]:
    rows = [
        [{"text": "⬆️ Upload Bot", "callback_data": "upload:start", "style": "success"},
         {"text": "🐙 Deploy from GitHub", "callback_data": "github:start", "style": "success"}],
        [{"text": "🤖 My Bots", "callback_data": "mybots:list", "style": "primary"},
         {"text": "📁 Files", "callback_data": "files:menu", "style": "primary"}],
        [{"text": "📜 Logs", "callback_data": "logs:menu", "style": "primary"},
         {"text": "🔑 Env Vars", "callback_data": "env:menu", "style": "primary"}],
        [{"text": "📊 Dashboard", "callback_data": "dashboard:show", "style": "primary"},
         {"text": "💾 Backup", "callback_data": "backup:menu", "style": "primary"}],
        [{"text": "⚙️ Settings", "callback_data": "settings:menu", "style": "primary"},
         {"text": "👤 Account", "callback_data": "account:show", "style": "primary"}],
        [{"text": "📢 Channel", "url": config.CREDIT_TELEGRAM_CHANNEL},
         {"text": "▶️ YouTube", "url": config.CREDIT_YOUTUBE_CHANNEL}],
    ]
    # Admin Panel button — only ever added to the layout for user ids in
    # config.ADMIN_IDS, so non-admins never even see it exists (not just
    # a permission check after the tap — the button itself isn't built).
    if user_id is not None and user_id in config.ADMIN_IDS:
        rows.insert(0, [{"text": "👑 Admin Panel", "callback_data": "admin:panel", "style": "danger"}])
    return rows


def main_menu_keyboard(user_id: int | None = None) -> InlineKeyboardMarkup:
    rows = []
    for row in _menu_rows(user_id):
        btn_row = []
        for b in row:
            if "url" in b:
                btn_row.append(InlineKeyboardButton(b["text"], url=b["url"]))
            else:
                btn_row.append(btn(b["text"], callback_data=b["callback_data"], style=b.get("style")))
        rows.append(btn_row)
    return InlineKeyboardMarkup(rows)


def home_caption(user_first_name: str) -> str:
    return (
        f"👋 Welcome, *{esc(user_first_name)}*!\n\n"
        "This bot lets you host your own Telegram bots (or any Python/Node app) "
        "straight from Telegram — upload a file, paste a GitHub link, and it's live.\n\n"
        "Use the menu below to get started."
        f"\n\n🔖 _v{config.VERSION}_"
        + config.CREDIT_FOOTER
    )


def _home_stats(user_id: int) -> dict:
    with bots_db() as conn:
        count = conn.execute("SELECT COUNT(*) c FROM bots WHERE owner_id=?", (user_id,)).fetchone()["c"]
    docker_ok = config.USE_DOCKER and docker_manager.is_docker_available()
    return {"bot_count": count, "docker_ok": docker_ok, "landlock_ok": sandbox.available()}


def _home_blocks(user_first_name: str, stats: dict, user_id: int | None = None) -> list:
    return [
        rm.video(config.CREDIT_HOME_VIDEO_URL),
        rm.heading("🚀 TELEGRAM HOSTING BOT"),
        rm.paragraph(
            f"Welcome, {user_first_name}! Host your own Telegram bots (or any Python/Node app) "
            "straight from Telegram — upload a file, paste a GitHub link, and it's live."
        ),
        rm.compact_table(
            ["Information", "Details"],
            [
                ["Owner", config.CREDIT_OWNER],
                ["Version", config.VERSION],
                ["Your bots", str(stats["bot_count"])],
                ["Sandbox", "Docker ✅" if stats["docker_ok"] else ("Landlock ✅" if stats.get("landlock_ok") else "NONE ❌ (deploy blocked)")],
            ],
        ),
        rm.paragraph("Please choose an option below 👇"),
        *[rm.button_row(row) for row in _menu_rows(user_id)],
    ]


def _ensure_user(user_id: int, username: str | None):
    with users_db() as conn:
        conn.execute(
            "INSERT INTO users (user_id, username, is_admin) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username",
            (user_id, username, 1 if user_id in config.ADMIN_IDS else 0),
        )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    _ensure_user(user.id, user.username)
    chat_id = update.effective_chat.id
    stats = _home_stats(user.id)

    # Attempt 1 (Bot API 10.3): rich blocks — video + heading + table +
    # in-message buttons, no separate reply_markup needed.
    try:
        await asyncio.to_thread(rm.send_blocks, chat_id, _home_blocks(user.first_name or "there", stats, user.id))
        return
    except rm.RichMessageError:
        pass

    # Attempt 2 (always works): plain video + Markdown caption + keyboard.
    caption = home_caption(user.first_name or "there")
    keyboard = main_menu_keyboard(user.id)
    try:
        await update.effective_chat.send_video(
            video=config.CREDIT_HOME_VIDEO_URL,
            caption=caption,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
    except Exception:
        # Fallback in case the video URL is unreachable
        await update.effective_chat.send_message(
            caption, parse_mode="Markdown", reply_markup=keyboard
        )


async def go_home_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    stats = _home_stats(user.id)

    # Attempt 1: edit the existing message in place using rich blocks.
    # No intro video here — editMessageText can't attach new media to a
    # message that didn't start with any, so the edited version omits
    # the video block (same approach as the Node.js sibling bot).
    try:
        await asyncio.to_thread(
            rm.edit_blocks,
            query.message.chat.id,
            query.message.message_id,
            _home_blocks(user.first_name or "there", stats, user.id)[1:],  # skip the video block
        )
        return
    except rm.RichMessageError:
        pass

    # Attempt 2: existing plain-text/caption edit path.
    caption = home_caption(user.first_name or "there")
    keyboard = main_menu_keyboard(user.id)
    await safe_edit(query, caption, reply_markup=keyboard)


def register(app):
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", start_command))  # /help == /start menu
    app.add_handler(CallbackQueryHandler(go_home_callback, pattern="^home:go$"))
