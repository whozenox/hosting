"""
handlers/admin.py
Admin-only commands:
  /vpsstatus              — whole-VPS CPU/RAM/disk/uptime
  /addprem <user_id> [days] — grant premium (omit days = permanent)
  /removeprem <user_id>     — revoke premium
  /premlist                 — list current premium users
  /setcpu <user_id> <cores> — override a user's CPU limit for new/rebuilt
                               containers (e.g. `/setcpu 123456789 2.5`)
  /resetcpu <user_id>       — clear the override, back to their tier default
                               (config.FREE_CPU_LIMIT / PREMIUM_CPU_LIMIT)

Plus the in-chat Admin Panel (callback_data "admin:*") — a menu of
colored ("rich") buttons that only renders for users in config.ADMIN_IDS
(handlers/home.py only adds the entry button to the menu for admins, and
every callback below re-checks _is_admin itself, so the panel can't be
reached even if someone guesses the callback_data). From the panel an
admin can see whole-VPS stats, the premium list, and — the main ask —
every hosted bot with its owner's user id and file/source, without
digging through the database by hand.
"""

import config
import asyncio
from telegram import Update, InlineKeyboardMarkup
from telegram.ext import ContextTypes, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from core import host_stats, premium, maintenance, limits, hosting_control, moderation, deploy
from database.db import bots_db, users_db
from handlers.common import esc, btn, safe_edit

BOTS_DB_COUNT_TIP = "Tip: use /premlist to see who currently has premium."


def _is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


def _vps_status_text() -> str:
    stats = host_stats.get_host_stats()
    load_line = ""
    if stats["load_avg"]:
        l1, l5, l15 = stats["load_avg"]
        load_line = f"Load avg: {l1:.2f}, {l5:.2f}, {l15:.2f}\n"

    with bots_db() as conn:
        running = conn.execute(
            "SELECT name, owner_id, started_at FROM bots WHERE status='running' ORDER BY started_at ASC LIMIT 10"
        ).fetchall()
        running_count = conn.execute("SELECT COUNT(*) c FROM bots WHERE status='running'").fetchone()["c"]

    text = (
        "🖥 *VPS Status*\n\n"
        f"CPU: {stats['cpu_percent']}% ({stats['cpu_cores']} cores)\n"
        f"{load_line}"
        f"RAM: {stats['mem_used_mb']}MB / {stats['mem_total_mb']}MB ({stats['mem_percent']}%)\n"
        f"Disk: {stats['disk_used_gb']}GB / {stats['disk_total_gb']}GB ({stats['disk_percent']}%)\n"
        f"Uptime: {host_stats.format_uptime(stats['uptime_seconds'])}\n\n"
        f"🟢 *{running_count} bot(s) currently running*"
    )
    if running:
        text += "\n" + "\n".join(
            f"  • `{esc(r['name'])}` (owner `{r['owner_id']}`) — online `{host_stats.bot_uptime_str(r['started_at'], 'running')}`"
            for r in running
        )
        if running_count > len(running):
            text += f"\n  _…and {running_count - len(running)} more (see All Hosted Bots)_"
    return text


def _premium_list_text() -> str:
    users = premium.list_premium_users()
    if not users:
        return "No premium users yet."
    lines = ["👑 *Premium users*\n"]
    for u in users:
        uname = f"@{esc(u['username'])}" if u["username"] else "(no username)"
        expiry = u["premium_until"] or "never"
        lines.append(f"`{u['user_id']}` {uname} — expires: `{expiry}`")
    return "\n".join(lines)


def _panel_keyboard() -> InlineKeyboardMarkup:
    maint_on = maintenance.is_on()
    maint_label = "🛠 Maintenance: ON (tap to turn OFF)" if maint_on else "🛠 Maintenance: OFF (tap to turn ON)"
    appr_on = hosting_control.get_approval_required()
    appr_label = "✅ Approval: ON (tap to turn OFF)" if appr_on else "⚡ Approval: OFF (tap to turn ON)"
    used, total = hosting_control.slot_status()
    slot_style = "danger" if used >= config.MAX_HOSTING_SLOTS else "primary"
    maint_style = "danger" if maint_on else "success"
    return InlineKeyboardMarkup([
        [btn("📊 VPS Status", callback_data="admin:vps", style="primary"),
         btn("👑 Premium List", callback_data="admin:premlist", style="primary")],
        [btn(f"🤖 Hosted Bots {used}/{total}", callback_data="admin:allbots", style=slot_style),
         btn("👥 All Users", callback_data="admin:allusers", style="primary")],
        [btn(appr_label, callback_data="admin:approval_toggle", style="danger" if appr_on else "success")],
        [btn("🛡 Moderation / Bans", callback_data="admin:moderation", style="danger"),
         btn("📢 Broadcast", callback_data="admin:broadcast", style="primary")],
        [btn("🔍 Scan Bot Files", callback_data="admin:scan_list", style="danger")],
        [btn(maint_label, callback_data="admin:maintenance_toggle", style=maint_style)],
        [btn("⬅️ Back to Menu", callback_data="home:go")],
    ])


async def _guard(update: Update) -> bool:
    """Returns True and answers with an alert if the caller isn't an admin."""
    query = update.callback_query
    user = update.effective_user
    if not _is_admin(user.id):
        await query.answer("⛔ Admins only.", show_alert=True)
        return False
    return True


def _panel_text() -> str:
    maint_line = "🛠 Maintenance mode is currently *ON* — only admins can use the bot.\n\n" if maintenance.is_on() else ""
    return (
        "👑 *Admin Panel*\n\n"
        f"{maint_line}"
        "Whole-VPS stats, premium management, every bot hosted on this "
        "server (who owns it and what it was deployed from), and the "
        "maintenance-mode switch."
        + config.CREDIT_FOOTER
    )


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _guard(update):
        return
    await query.answer()
    await safe_edit(query, _panel_text(), parse_mode="Markdown", reply_markup=_panel_keyboard())


async def admin_maintenance_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _guard(update):
        return
    new_state = maintenance.toggle()
    await query.answer(
        "🛠 Maintenance turned ON — only admins can use the bot now." if new_state
        else "✅ Maintenance turned OFF — the bot is live for everyone again.",
        show_alert=True,
    )
    # Re-render the panel in place (without calling query.answer() again —
    # a callback query can only be answered once).
    await safe_edit(query, _panel_text(), parse_mode="Markdown", reply_markup=_panel_keyboard())


async def admin_vps_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _guard(update):
        return
    await query.answer()
    kb = InlineKeyboardMarkup([[btn("⬅️ Back", callback_data="admin:panel")]])
    await safe_edit(query, (await asyncio.to_thread(_vps_status_text)) + config.CREDIT_FOOTER, parse_mode="Markdown", reply_markup=kb)


async def admin_premlist_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _guard(update):
        return
    await query.answer()
    kb = InlineKeyboardMarkup([[btn("⬅️ Back", callback_data="admin:panel")]])
    await safe_edit(query, _premium_list_text() + config.CREDIT_FOOTER, parse_mode="Markdown", reply_markup=kb)


async def admin_allbots_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _guard(update):
        return
    await query.answer()
    with bots_db() as conn:
        rows = conn.execute(
            "SELECT bot_id, owner_id, name, source, runtime, status, created_at, started_at "
            "FROM bots ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
    if not rows:
        kb = InlineKeyboardMarkup([[btn("⬅️ Back", callback_data="admin:panel")]])
        await safe_edit(query, "📭 No bots hosted yet." + config.CREDIT_FOOTER, reply_markup=kb)
        return
    status_icon = {"running": "🟢", "stopped": "⚪", "crashed": "🔴", "deploying": "🟡"}
    lines = [f"🤖 *Hosted bots* ({len(rows)})\n"]
    buttons = []
    for r in rows:
        icon = status_icon.get(r["status"], "⚪")
        online_for = host_stats.bot_uptime_str(r["started_at"], r["status"])
        lines.append(
            f"{icon} `#{r['bot_id']}` *{esc(r['name'])}*\n"
            f"    👤 owner `{r['owner_id']}` · {r['source']}/{r['runtime'] or '?'} · {r['status']}\n"
            f"    ⏱ `{online_for}`"
        )
        buttons.append([btn(f"⚙️ Manage #{r['bot_id']}", callback_data=f"admin:bot:{r['bot_id']}", style="primary")])
    buttons.append([btn("⬅️ Back", callback_data="admin:panel")])
    await safe_edit(query, "\n".join(lines) + config.CREDIT_FOOTER,
                    parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))


async def admin_allusers_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _guard(update):
        return
    await query.answer()

    with users_db() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        premium_count = conn.execute("SELECT COUNT(*) c FROM users WHERE is_premium=1").fetchone()["c"]
        recent = conn.execute(
            "SELECT user_id, username, first_seen, is_premium, cpu_limit FROM users ORDER BY first_seen DESC LIMIT 20"
        ).fetchall()
        custom_cpu = conn.execute(
            "SELECT user_id, username, cpu_limit FROM users WHERE cpu_limit IS NOT NULL"
        ).fetchall()

    lines = [
        "👥 *All Users*\n",
        f"Total: `{total}`  ·  Premium: `{premium_count}`  ·  Admins: `{len(config.ADMIN_IDS)}`\n",
        f"CPU defaults — free: `{config.FREE_CPU_LIMIT:g}` · premium: `{config.PREMIUM_CPU_LIMIT:g}` · "
        f"admin: `{config.ADMIN_CPU_LIMIT:g}` cores\n",
        "_Most recently seen:_",
    ]
    for u in recent:
        uname = f"@{esc(u['username'])}" if u["username"] else "(no username)"
        prem = " 👑" if u["is_premium"] else ""
        cpu = f" · {u['cpu_limit']:g} cores (custom)" if u["cpu_limit"] is not None else ""
        lines.append(f"`{u['user_id']}` {uname}{prem}{cpu}")

    if custom_cpu:
        lines.append("\n_CPU overrides set:_")
        for u in custom_cpu:
            uname = f"@{esc(u['username'])}" if u["username"] else "(no username)"
            lines.append(f"`{u['user_id']}` {uname} → `{u['cpu_limit']:g}` cores")

    lines.append(
        "\nUse `/setcpu <user_id> <cores>` to override a user's CPU limit, "
        "or `/resetcpu <user_id>` to clear it."
    )

    kb = InlineKeyboardMarkup([[btn("⬅️ Back", callback_data="admin:panel")]])
    await safe_edit(query, "\n".join(lines) + config.CREDIT_FOOTER, parse_mode="Markdown", reply_markup=kb)


async def admin_approval_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _guard(update):
        return
    new_state = not hosting_control.get_approval_required()
    hosting_control.set_approval_required(new_state)
    await query.answer("Approval ON" if new_state else "Approval OFF", show_alert=True)
    await safe_edit(query, _panel_text(), parse_mode="Markdown", reply_markup=_panel_keyboard())


async def admin_moderation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _guard(update):
        return
    await query.answer()
    rows = moderation.list_moderation_users(30)
    if not rows:
        text = "🛡 *Moderation*\n\nNo warnings or bans recorded." + config.CREDIT_FOOTER
        kb = InlineKeyboardMarkup([[btn("⬅️ Back", callback_data="admin:panel")]])
        await safe_edit(query, text, parse_mode="Markdown", reply_markup=kb)
        return
    buttons = []
    lines = ["🛡 *Moderation / Security Warnings*\n"]
    for r in rows:
        uname = f"@{esc(r['username'])}" if r["username"] else "no username"
        status = "⛔ BANNED" if r["is_banned"] else "⚠️ WARNING"
        lines.append(f"`{r['user_id']}` {uname} — {status} `{r['security_warnings']}/{config.MAX_SECURITY_WARNINGS}`")
        action = "Unban" if r["is_banned"] else "Ban"
        action_id = "unban" if r["is_banned"] else "ban"
        buttons.append([btn(f"{'♻️' if r['is_banned'] else '⛔'} {action} {r['user_id']}",
                            callback_data=f"admin:{action_id}:{r['user_id']}",
                            style="success" if r["is_banned"] else "danger")])
    buttons.append([btn("⬅️ Back", callback_data="admin:panel")])
    await safe_edit(query, "\n".join(lines) + config.CREDIT_FOOTER,
                    parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))


async def admin_ban_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _guard(update):
        return
    parts = query.data.split(":")
    action, target = parts[1], int(parts[2])
    ok, msg = moderation.set_ban(target, action == "ban", "Admin moderation")
    await query.answer(msg, show_alert=True)
    await admin_moderation(update, context)


async def admin_bot_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _guard(update):
        return
    bot_id = int(query.data.split(":")[2])
    with bots_db() as conn:
        row = conn.execute("SELECT * FROM bots WHERE bot_id=?", (bot_id,)).fetchone()
    if not row:
        await query.answer("Bot not found.", show_alert=True)
        return
    row = dict(row)
    status = row["status"]
    buttons = []
    if status == "running":
        buttons.append([btn("⏹ Stop", callback_data=f"admin:botstop:{bot_id}", style="danger")])
    else:
        buttons.append([btn("▶️ Start / Restart", callback_data=f"admin:botstart:{bot_id}", style="success")])
    buttons.append([btn("🗑 Delete Bot", callback_data=f"admin:botdelete:{bot_id}", style="danger")])
    buttons.append([btn("⬅️ Back", callback_data="admin:allbots")])
    text = (
        f"🤖 *{esc(row['name'])}*\n"
        f"ID: `{row['bot_id']}`\nOwner: `{row['owner_id']}`\n"
        f"Runtime: `{row['runtime']}`\nStatus: `{status}`\n"
        f"Source: `{row['source']}`"
        + config.CREDIT_FOOTER
    )
    await query.answer()
    await safe_edit(query, text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))


async def admin_bot_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not await _guard(update):
        return
    action = query.data.split(":")[1].replace("bot", "", 1)
    bot_id = int(query.data.split(":")[2])
    with bots_db() as conn:
        row = conn.execute("SELECT * FROM bots WHERE bot_id=?", (bot_id,)).fetchone()
    if not row:
        await query.answer("Bot not found.", show_alert=True)
        return
    row = dict(row)
    try:
        if action == "stop":
            await asyncio.to_thread(deploy.stop_bot, row)
            msg = "Bot stopped."
        elif action == "start":
            await deploy.restart_bot_async(row)
            msg = "Bot started/restarted."
        else:
            await asyncio.to_thread(deploy.delete_bot, row)
            msg = "Bot deleted."
    except Exception as e:
        await query.answer(f"Failed: {e}"[:190], show_alert=True)
        return
    await query.answer(msg, show_alert=True)
    # Re-render all hosted bots after action.
    await admin_allbots_callback(update, context)


async def broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id):
        if update.callback_query:
            await update.callback_query.answer("Admins only.", show_alert=True)
        return
    if update.callback_query:
        await update.callback_query.answer()
        context.user_data["admin_broadcast"] = True
        await update.callback_query.message.reply_text(
            "📢 *Broadcast mode ON*\nSend the message/media you want to broadcast.\n"
            "Send `/cancelbroadcast` to cancel.", parse_mode="Markdown"
        )
    else:
        context.user_data["admin_broadcast"] = True
        await update.message.reply_text(
            "📢 Broadcast mode ON. Send the message/media now. `/cancelbroadcast` to cancel."
        )


async def cancel_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if _is_admin(update.effective_user.id):
        context.user_data["admin_broadcast"] = False
        await update.message.reply_text("✅ Broadcast cancelled.")


async def broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user is None or context.user_data is None:
        return  # channel post / anonymous admin: no user to check
    if not context.user_data.get("admin_broadcast") or not _is_admin(user.id):
        return
    context.user_data["admin_broadcast"] = False
    with users_db() as conn:
        user_ids = [r["user_id"] for r in conn.execute("SELECT user_id FROM users").fetchall()]
    sent = failed = 0
    for uid in user_ids:
        try:
            await context.bot.copy_message(
                chat_id=uid, from_chat_id=update.effective_chat.id,
                message_id=update.effective_message.message_id
            )
            sent += 1
        except Exception:
            failed += 1
        if config.BROADCAST_DELAY_SECONDS:
            await asyncio.sleep(config.BROADCAST_DELAY_SECONDS)
    await update.message.reply_text(
        f"📢 Broadcast complete.\n\n✅ Sent: `{sent}`\n❌ Failed: `{failed}`",
        parse_mode="Markdown"
    )


async def vps_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ This command is for admins only.")
        return

    await update.message.reply_text((await asyncio.to_thread(_vps_status_text)) + config.CREDIT_FOOTER, parse_mode="Markdown")


async def add_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ This command is for admins only.")
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: `/addprem <user_id> [days]`\n"
            "Example: `/addprem 123456789 30` (30-day premium)\n"
            "Omit days for permanent premium: `/addprem 123456789`",
            parse_mode="Markdown",
        )
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ user_id must be a number.")
        return

    days = None
    if len(context.args) > 1:
        try:
            days = int(context.args[1])
        except ValueError:
            await update.message.reply_text("❌ days must be a number.")
            return

    premium.ensure_user_row(target_id)
    until = premium.grant_premium(target_id, days)

    if until:
        await update.message.reply_text(
            f"✅ Granted premium to `{target_id}` until `{until}` (UTC)." + config.CREDIT_FOOTER,
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(
            f"✅ Granted permanent premium to `{target_id}`." + config.CREDIT_FOOTER,
            parse_mode="Markdown",
        )

    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=(
                "🎉 You've been upgraded to *Premium*!\n"
                f"You can now host up to {config.MAX_BOTS_PER_PREMIUM_USER} bots."
                + config.CREDIT_FOOTER
            ),
            parse_mode="Markdown",
        )
    except Exception:
        pass  # user may have never started the bot in DM


async def remove_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ This command is for admins only.")
        return

    if not context.args:
        await update.message.reply_text("Usage: `/removeprem <user_id>`", parse_mode="Markdown")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ user_id must be a number.")
        return

    premium.revoke_premium(target_id)
    await update.message.reply_text(f"✅ Revoked premium from `{target_id}`." + config.CREDIT_FOOTER, parse_mode="Markdown")


async def premium_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ This command is for admins only.")
        return

    await update.message.reply_text(_premium_list_text() + config.CREDIT_FOOTER, parse_mode="Markdown")


async def set_cpu_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ This command is for admins only.")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: `/setcpu <user_id> <cores>`\n"
            "Example: `/setcpu 123456789 2.5` (2.5 CPU cores for that user's bots)\n"
            "This overrides their free/premium tier default until `/resetcpu`.\n"
            "Only affects Docker-mode containers, applied on the next deploy/rebuild.",
            parse_mode="Markdown",
        )
        return

    try:
        target_id = int(context.args[0])
        cores = float(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ user_id and cores must be numbers, e.g. `/setcpu 123456789 2`", parse_mode="Markdown")
        return

    if cores <= 0:
        await update.message.reply_text("❌ Cores must be greater than 0.")
        return

    premium.ensure_user_row(target_id)
    limits.set_custom_limit(target_id, cores)
    await update.message.reply_text(
        f"✅ CPU limit for `{target_id}` set to `{cores:g}` cores.\n"
        "Takes effect on that user's next deploy, restart, or auto-fix rebuild."
        + config.CREDIT_FOOTER,
        parse_mode="Markdown",
    )


async def reset_cpu_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ This command is for admins only.")
        return

    if not context.args:
        await update.message.reply_text("Usage: `/resetcpu <user_id>`", parse_mode="Markdown")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ user_id must be a number.")
        return

    limits.set_custom_limit(target_id, None)
    default = limits.tier_default(target_id, is_admin=target_id in config.ADMIN_IDS)
    await update.message.reply_text(
        f"✅ CPU override cleared for `{target_id}` — back to their tier default of `{default:g}` cores."
        + config.CREDIT_FOOTER,
        parse_mode="Markdown",
    )


def register(app):
    app.add_handler(CommandHandler("vpsstatus", vps_status))
    app.add_handler(CommandHandler("addprem", add_premium))
    app.add_handler(CommandHandler("removeprem", remove_premium))
    app.add_handler(CommandHandler("premlist", premium_list))
    app.add_handler(CommandHandler("setcpu", set_cpu_limit))
    app.add_handler(CommandHandler("resetcpu", reset_cpu_limit))

    # In-chat Admin Panel (button-driven; see handlers/home.py for the
    # entry point, which only shows the button at all to config.ADMIN_IDS).
    app.add_handler(CallbackQueryHandler(admin_panel, pattern="^admin:panel$"))
    app.add_handler(CallbackQueryHandler(admin_vps_callback, pattern="^admin:vps$"))
    app.add_handler(CallbackQueryHandler(admin_premlist_callback, pattern="^admin:premlist$"))
    app.add_handler(CallbackQueryHandler(admin_allbots_callback, pattern="^admin:allbots$"))
    app.add_handler(CallbackQueryHandler(admin_allusers_callback, pattern="^admin:allusers$"))
    app.add_handler(CallbackQueryHandler(admin_maintenance_toggle, pattern="^admin:maintenance_toggle$"))
    app.add_handler(CallbackQueryHandler(admin_approval_toggle, pattern="^admin:approval_toggle$"))
    app.add_handler(CallbackQueryHandler(admin_moderation, pattern="^admin:moderation$"))
    app.add_handler(CallbackQueryHandler(admin_ban_toggle, pattern=r"^admin:(ban|unban):\d+$"))
    app.add_handler(CallbackQueryHandler(admin_bot_view, pattern=r"^admin:bot:\d+$"))
    app.add_handler(CallbackQueryHandler(admin_bot_action, pattern=r"^admin:bot(start|stop|delete):\d+$"))
    app.add_handler(CallbackQueryHandler(admin_scan_list, pattern="^admin:scan_list$"))
    app.add_handler(CallbackQueryHandler(admin_scan_run,  pattern=r"^admin:scan:\d+$"))
    app.add_handler(CallbackQueryHandler(broadcast_start, pattern="^admin:broadcast$"))
    app.add_handler(CommandHandler("broadcast", broadcast_start))
    app.add_handler(CommandHandler("cancelbroadcast", cancel_broadcast))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, broadcast_message, block=False), group=1)


# ══════════════════════════════════════════════════════════════
#  ADMIN SCAN — kisi bhi bot ki files manually scan karo
# ══════════════════════════════════════════════════════════════

async def admin_scan_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin panel me 'Scan Bot Files' button => sab bots list."""
    query = update.callback_query
    if not await _guard(update):
        return
    await query.answer()
    with bots_db() as conn:
        rows = conn.execute(
            "SELECT bot_id, owner_id, name, status FROM bots ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
    if not rows:
        kb = InlineKeyboardMarkup([[btn("⬅️ Back", callback_data="admin:panel")]])
        await safe_edit(query, "📭 Koi bot hosted nahi hai." + config.CREDIT_FOOTER, reply_markup=kb)
        return
    status_icon = {"running": "🟢", "stopped": "⚪", "crashed": "🔴", "deploying": "🟡"}
    buttons = []
    for r in rows:
        icon = status_icon.get(r["status"], "⚪")
        buttons.append([btn(
            f"{icon} #{r['bot_id']} {r['name']} (owner {r['owner_id']})",
            callback_data=f"admin:scan:{r['bot_id']}"
        )])
    buttons.append([btn("⬅️ Back", callback_data="admin:panel")])
    await safe_edit(query,
        "🔍 *Scan Bot Files*\n\nKis bot ko scan karna hai?",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))


async def admin_scan_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ek bot ki files scan karke result dikhao."""
    query = update.callback_query
    if not await _guard(update):
        return
    await query.answer("🔍 Scanning...")
    bot_id = int(query.data.split(":")[2])
    with bots_db() as conn:
        row = conn.execute("SELECT * FROM bots WHERE bot_id=?", (bot_id,)).fetchone()
    if not row:
        await query.answer("Bot not found.", show_alert=True)
        return
    row = dict(row)
    await safe_edit(query, f"🔍 Scanning `{esc(row['name'])}`..." + config.CREDIT_FOOTER, parse_mode="Markdown")

    import os
    from core import security as sec

    project_path = row.get("path", "")
    if not project_path or not os.path.isdir(project_path):
        kb = InlineKeyboardMarkup([[btn("⬅️ Back", callback_data="admin:scan_list")]])
        await safe_edit(query, "❌ Bot folder nahi mila (delete ho gaya?)." + config.CREDIT_FOOTER, reply_markup=kb)
        return

    ok, reason = await asyncio.to_thread(sec.scan_project_tree, project_path)

    if ok:
        result_text = (
            f"✅ *Scan Clean!*\n\n"
            f"🤖 Bot: `{esc(row['name'])}` (#{bot_id})\n"
            f"👤 Owner: `{row['owner_id']}`\n"
            f"📂 Path: `{esc(project_path)}`\n\n"
            f"Koi malicious code nahi mila."
        )
    else:
        result_text = (
            f"🚨 *Malware Found!*\n\n"
            f"🤖 Bot: `{esc(row['name'])}` (#{bot_id})\n"
            f"👤 Owner: `{row['owner_id']}`\n"
            f"📂 Path: `{esc(project_path)}`\n\n"
            f"❌ *Reason:*\n`{esc(reason)}`\n\n"
            f"_Ban karne ke liye Moderation panel use karo._"
        )

    kb = InlineKeyboardMarkup([
        [btn("🗑 Bot Delete", callback_data=f"admin:botdelete:{bot_id}", style="danger"),
         btn("⛔ Owner Ban", callback_data=f"admin:ban:{row['owner_id']}", style="danger")],
        [btn("🔄 Dobara Scan", callback_data=f"admin:scan:{bot_id}")],
        [btn("⬅️ Back", callback_data="admin:scan_list")],
    ])
    await safe_edit(query, result_text + config.CREDIT_FOOTER, parse_mode="Markdown", reply_markup=kb)
