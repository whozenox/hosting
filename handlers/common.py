"""
handlers/common.py
Shared helper for editing the bot's menu message.

/start sends a VIDEO with a caption, so every subsequent inline button
press is editing that same message. Telegram's API treats caption
edits and text edits as different calls — calling edit_message_text()
on a media message raises:
    telegram.error.BadRequest: There is no text in the message to edit

safe_edit() detects which kind of message we're dealing with and calls
the right method, falling back to deleting + resending if neither edit
is possible (e.g. the message is too old, or was a document).
"""

from telegram.error import BadRequest
from telegram import InlineKeyboardButton
import re


def btn(text, callback_data=None, style=None, **kwargs) -> InlineKeyboardButton:
    """
    Drop-in replacement for InlineKeyboardButton(..., style=...).

    The colored `style` field (Bot API 9.4) is only accepted by
    python-telegram-bot >= 22.7. On any older version installed on the
    server, passing style= raises TypeError and the whole handler crashes
    (button looks like it "does nothing" to the user, since the message
    never gets edited). This tries with style first and silently falls
    back to a plain button if the installed PTB version doesn't support it.
    """
    if style is not None:
        try:
            return InlineKeyboardButton(text, callback_data=callback_data, style=style, **kwargs)
        except TypeError:
            pass
    return InlineKeyboardButton(text, callback_data=callback_data, **kwargs)


def esc(value) -> str:
    """
    Escape characters that have special meaning in Telegram's legacy
    Markdown parser (*, _, `, [) so that user-supplied text (usernames,
    bot names, filenames, repo URLs) can't break message parsing or
    accidentally inject formatting.
    """
    return re.sub(r"([_*`\[])", r"\\\1", str(value))


async def safe_edit(query, text, reply_markup=None, parse_mode="Markdown"):
    """
    Edits the message behind a button press, whatever kind it is
    (plain text / media caption / rich message). Order of attempts:
      1. the edit method matching the message type, with Markdown
      2. the other edit method (rich/video menus report no caption/text)
      3. same again as plain text (in case Markdown was malformed)
      4. delete + send a fresh message (old message, too long for a
         caption, can't be edited, ...)
    """
    msg = query.message
    is_media = bool(
        getattr(msg, "caption", None) is not None
        or getattr(msg, "photo", None) or getattr(msg, "video", None)
        or getattr(msg, "document", None) or getattr(msg, "animation", None)
    )
    limit = 1024 if is_media else 4096

    if len(text) <= limit:
        methods = ("caption", "text") if is_media else ("text", "caption")
        for pm in (parse_mode, None):
            for method in methods:
                try:
                    if method == "caption":
                        await query.edit_message_caption(caption=text, parse_mode=pm, reply_markup=reply_markup)
                    else:
                        await query.edit_message_text(text, parse_mode=pm, reply_markup=reply_markup)
                    return
                except BadRequest as e:
                    err = str(e).lower()
                    if "not modified" in err:
                        return
                    continue
                except Exception:
                    continue

    # Last resort: delete and send a fresh message (max 4096 chars).
    if len(text) > 4000:
        text = text[:3990].rstrip() + "\n…"
    try:
        await msg.delete()
    except Exception:
        pass
    chat = msg.chat
    try:
        await chat.send_message(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except BadRequest:
        await chat.send_message(text, parse_mode=None, reply_markup=reply_markup)
