"""
core/rich_message.py
Bot API 10.3 (Aug 24, 2026) "rich message" helpers: heading / paragraph /
compact table / video / in-message buttons — the same building blocks used
on the Node.js sibling bot's start screen.

python-telegram-bot 22.7 may not have a typed wrapper for `sendRichMessage`
yet (it's a very new API surface), so this talks to Telegram's HTTP API
directly with `requests` (already a project dependency) instead of waiting
on PTB. Every call site that uses this module MUST fall back to a plain
PTB call (send_video/send_message, edit_message_caption/text) if the rich
call raises — see handlers/home.py for the pattern. That way a field-name
mismatch here never breaks the bot, it just loses the fancy formatting.
"""

import requests
import config

TELEGRAM_API = f"https://api.telegram.org/bot{config.BOT_TOKEN}"
_TIMEOUT = 10


class RichMessageError(Exception):
    pass


def _call_api(method: str, payload: dict) -> dict:
    try:
        resp = requests.post(f"{TELEGRAM_API}/{method}", json=payload, timeout=_TIMEOUT)
        data = resp.json()
    except Exception as e:
        raise RichMessageError(f"{method} request failed: {e}")
    if not data.get("ok"):
        raise RichMessageError(data.get("description", f"{method} failed"))
    return data["result"]


# --- block builders ---------------------------------------------------

def heading(text: str, size: int = 2) -> dict:
    return {"type": "heading", "text": text, "size": size}


def paragraph(text: str) -> dict:
    return {"type": "paragraph", "text": text}


def video(url: str) -> dict:
    return {"type": "video", "video": {"type": "video", "media": url}}


def compact_table(header_row: list[str], data_rows: list[list[str]]) -> dict:
    def cell(text, is_header=False):
        c = {"text": str(text), "align": "left", "valign": "middle"}
        if is_header:
            c["is_header"] = True
        return c

    return {
        "type": "table",
        "is_compact": True,
        "cells": [[cell(t, True) for t in header_row]] + [[cell(t) for t in row] for row in data_rows],
    }


def button_row(buttons: list[dict]) -> dict:
    """Each button dict: {"text": ..., "url": ...} or {"text": ..., "callback_data": ..., "style": ...}"""
    out = []
    for b in buttons:
        item = {"text": b["text"]}
        if b.get("url"):
            item["url"] = b["url"]
        if b.get("callback_data"):
            item["callback_data"] = b["callback_data"]
        if b.get("style"):
            item["style"] = b["style"]
        out.append(item)
    return {"type": "buttons", "buttons": out}


# --- send / edit ---------------------------------------------------------

def send_blocks(chat_id, blocks: list) -> dict:
    return _call_api("sendRichMessage", {"chat_id": chat_id, "rich_message": {"blocks": blocks}})


def edit_blocks(chat_id, message_id, blocks: list) -> dict:
    return _call_api(
        "editMessageText",
        {"chat_id": chat_id, "message_id": message_id, "rich_message": {"blocks": blocks}},
    )
