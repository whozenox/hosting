"""
core/force_join.py
Checks whether a user has joined every channel/group listed in
config.FORCE_JOIN_CHATS, and builds the "please join first" prompt.

The bot must be an admin in each chat for get_chat_member() to work —
if it isn't (misconfiguration), that chat is skipped with a warning
instead of locking every user out of the bot.
"""

import time
import asyncio
import logging
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import TelegramError
import config

logger = logging.getLogger("bot.force_join")

NOT_MEMBER_STATUSES = {"left", "kicked"}


# user_id -> unix time until which "has joined everything" is trusted.
# The gate runs on EVERY update; without this each button tap cost one
# getChatMember API call per required chat, which (with many users) was
# slow and hit Telegram's rate limits.
_JOINED_CACHE: dict[int, float] = {}
_JOINED_TTL = 300  # seconds


async def _is_member(bot, chat: dict, user_id: int):
    """True = member, False = not a member, None = couldn't check (skip)."""
    try:
        member = await bot.get_chat_member(chat["chat_id"], user_id)
    except TelegramError as e:
        # Bot isn't admin there / chat_id typo / chat doesn't exist —
        # don't let a misconfigured entry brick the whole bot.
        logger.warning("Force-join check skipped for %s: %s", chat["chat_id"], e)
        return None
    return member.status not in NOT_MEMBER_STATUSES


async def get_unjoined_chats(bot, user_id: int, use_cache: bool = True) -> list[dict]:
    """Returns the list of config.FORCE_JOIN_CHATS entries the user hasn't joined."""
    if not config.FORCE_JOIN_ENABLED or not config.FORCE_JOIN_CHATS:
        return []

    if use_cache and _JOINED_CACHE.get(user_id, 0) > time.time():
        return []

    results = await asyncio.gather(*[_is_member(bot, c, user_id) for c in config.FORCE_JOIN_CHATS])
    unjoined = [c for c, ok in zip(config.FORCE_JOIN_CHATS, results) if ok is False]
    if not unjoined:
        if len(_JOINED_CACHE) > 20000:
            _JOINED_CACHE.clear()
        _JOINED_CACHE[user_id] = time.time() + _JOINED_TTL
    else:
        _JOINED_CACHE.pop(user_id, None)
    return unjoined


def build_prompt(unjoined: list[dict]) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "🔒 *Access locked*\n\n"
        "Please join the following before using this bot:"
    )
    rows = [[InlineKeyboardButton(f"📢 {c['name']}", url=c["url"])] for c in unjoined]
    rows.append([InlineKeyboardButton("✅ I've Joined", callback_data="forcejoin:check")])
    return text, InlineKeyboardMarkup(rows)
