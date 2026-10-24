"""
config.py
All configuration and credits for the Telegram Hosting Bot.
No .env files, no os.getenv() — everything lives here.
Edit the values below before running the bot.
"""

# ─── Core Bot Settings ────────────────────────────────────────────────
# ⚠️ Paste your FULL token from @BotFather here. It looks like:
#     123456789:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
# The old value was only the numeric bot-id half — not a usable token.
import os as _os_env
# Hosting platform ke Secrets/Env (Koyeb, Railway, VPS) se token lo — config.py
# me token likhna zaroori nahi. Env var BOT_TOKEN set ho to wahi use hoga.
BOT_TOKEN = _os_env.environ.get("8952251204:AAHCQAcSLMTAmuK889AVuJaCsskVwgF2Xh8") or "8952251204:AAHCQAcSLMTAmuK889AVuJaCsskVwgF2Xh8"

OWNER_ID = 8112149031

ADMIN_IDS = [
    8112149031
]

# ─── Version ───────────────────────────────────────────────────────────
VERSION = "5.3-ptero-sandbox"

# ─── Credits ───────────────────────────────────────────────────────────
CREDIT_OWNER = "@lordzenox"

CREDIT_TELEGRAM_CHANNEL = "https://t.me/zenoxtool"

CREDIT_YOUTUBE_CHANNEL = "https://www.youtube.com/"

CREDIT_HOME_VIDEO_URL = ""

CREDIT_FOOTER = f"\n\n👑 Bot by {CREDIT_OWNER}"

# ─── Paths ─────────────────────────────────────────────────────────────
import os as _os

BASE_DIR = _os.path.dirname(_os.path.abspath(__file__))

UPLOADS_DIR = _os.path.join(BASE_DIR, "uploads")
CONTAINERS_DIR = _os.path.join(BASE_DIR, "containers")
BACKUPS_DIR = _os.path.join(BASE_DIR, "backups")
LOGS_DIR = _os.path.join(BASE_DIR, "logs")
TEMP_DIR = _os.path.join(BASE_DIR, "temp")
DATABASE_DIR = _os.path.join(BASE_DIR, "database")

USERS_DB = _os.path.join(DATABASE_DIR, "users.db")
BOTS_DB = _os.path.join(DATABASE_DIR, "bots.db")
SETTINGS_DB = _os.path.join(DATABASE_DIR, "settings.db")

# ─── Deployment / Runtime Settings ─────────────────────────────────────
# SECURITY: Docker isolation is mandatory for untrusted user projects.
# Deployments are rejected if Docker is unavailable; never fall back to host subprocesses.

# ⚠️ Left OFF on purpose. Every Docker build pulls/creates a
# python:3.11-slim (~150MB) or node:22-slim (~200MB) image PER BOT, and
# every force-rebuild (auto-fix, every crash-loop restart, up to
# MAX_AUTO_RESTARTS times) builds ANOTHER full image on top of the old
# one. On a small host (Koyeb's free/starter instances included — they
# don't give you a usable Docker socket anyway) that alone blows past a
# couple hundred MB in hours. Keep this False unless you're on a host
# with real Docker-in-Docker support and several GB of disk to spare.
USE_DOCKER = True

DOCKER_IMAGE_PYTHON = "python:3.11-slim"
DOCKER_IMAGE_NODE = "node:22-slim"

# Per-bot resource limits (only enforced when USE_DOCKER = True)
CONTAINER_CPU_LIMIT = 1.0        # CPU cores — fallback used if a tier default below isn't set
CONTAINER_MEMORY_LIMIT = "1g"  # RAM

# Per-user CPU limits (in CPU cores, e.g. 0.5, 1, 2) for hosted bot
# containers — how much CPU each of a user's bots may use. An admin can
# still override any single user's limit from the Admin Panel / /setcpu,
# which always wins over these tier defaults.
FREE_CPU_LIMIT = 1.0
PREMIUM_CPU_LIMIT = 2.0
ADMIN_CPU_LIMIT = 4.0

# Max upload size in megabytes. Sized against MAX_STORAGE_MB below — one
# huge upload shouldn't be able to eat most of the whole box by itself.
MAX_UPLOAD_MB = 500

# Max number of bots a single (non-admin) user may host at once
MAX_BOTS_PER_USER = 3

# Max number of bots a premium (non-admin) user may host at once
MAX_BOTS_PER_PREMIUM_USER = 15

# Number of log lines shown by default in the Logs menu
LOG_TAIL_LINES = 100

# Auto-restart a crashed bot up to N times before giving up
MAX_AUTO_RESTARTS = 5

# ─── Storage Budget (new) ───────────────────────────────────────────────
# Total disk this whole panel (uploads/ + backups/ + logs/ + temp/ +
# any Docker images it built) is allowed to use. core/storage_guard.py
# enforces this: new uploads are rejected once it would push usage past
# this limit, and the background cleanup job (run from core/monitor.py
# and once on startup) prunes temp files, oversized logs, old backups
# and dangling Docker images to try to stay under it.
#
# Koyeb instance here has 2GB (2000MB) total disk — set to 1500MB, not
# 2000MB, on purpose: the OS, Python itself, pip-installed packages
# (python-telegram-bot, GitPython, psutil, etc.) and each hosted bot's
# own venv/node_modules already eat a few hundred MB before this panel's
# own uploads/backups/logs even start counting. Leaving ~500MB of
# headroom keeps the whole instance from hitting 100% disk (which would
# crash everything, not just reject a new upload) even before
# EMERGENCY_STORAGE_PERCENT below kicks in.
MAX_STORAGE_MB = 8500
# Never trust the number above blindly: cap it at 70% of the REAL disk so a
# small host (2GB Koyeb etc.) can't be "within budget" while actually full.
try:
    import shutil as _shutil
    _real_mb = _shutil.disk_usage(BASE_DIR).total / (1024 * 1024)
    MAX_STORAGE_MB = int(min(MAX_STORAGE_MB, _real_mb * 0.70))
except Exception:
    pass
# pip refuses to start an install if less than this much disk is free.
MIN_FREE_DISK_MB = 800

# Each per-bot log file (process-fallback mode) is truncated back to
# this size (keeping the newest bytes) the moment it would exceed it —
# logs were previously opened in append-forever mode with no cap.
LOG_MAX_KB = 300

# A backup zip is deleted right after it's sent to the user — it already
# lives in their Telegram chat, so there's no reason to keep a second
# copy taking up disk on the host.
BACKUP_AUTO_DELETE = True

# ─── Emergency Storage Recovery (new) ───────────────────────────────────
# If usage crosses this % of MAX_STORAGE_MB (or the real host disk, in
# case something outside uploads/backups/logs/temp is eating space —
# pip/npm caches, OS files), core/storage_guard.py stops doing routine
# cleanup and does an AGGRESSIVE one instead: every backup zip is deleted
# immediately (not just ones older than 10 min), and every log is
# truncated all the way down to EMERGENCY_LOG_MAX_KB. Goal: never let the
# disk actually hit 100% and crash the whole bot (and everyone's hosted
# bots with it) — losing some log history is a fair trade for staying up.
EMERGENCY_STORAGE_PERCENT = 90
EMERGENCY_LOG_MAX_KB = 50

# ─── Force Join (channel / group) ───────────────────────────────────────
# If True, a user must be a member of every chat listed below before they
# can use ANY command or button. Admins (ADMIN_IDS) always bypass this.
#
# IMPORTANT: the bot must be an ADMIN in every channel/group listed here
# (Telegram only lets bots check membership in chats they administer).
# If the bot isn't admin somewhere, that one entry is skipped automatically
# (with a warning in the console) instead of locking everyone out.
#
# chat_id: use the numeric id (e.g. -1001234567890) or the @username for
#          public channels/groups. url: the invite link shown on the button.
FORCE_JOIN_ENABLED = True

FORCE_JOIN_CHATS = [
    {"name": "LORDZENOx", "chat_id": "@zenoxtool", "url": CREDIT_TELEGRAM_CHANNEL},
     {"name": "Support Group", "chat_id": "@zenoxgc", "url": "https://t.me/zenoxgc"},
]


# ─── Hosting Capacity / Safety Controls ─────────────────────────────────
# Global active-hosting slots for normal users. Owners/admins bypass this
# limit and can host without a slot cap.
MAX_HOSTING_SLOTS = 200
SLOT_WARNING_AT = 40

# When enabled, normal-user uploads/GitHub deploys wait for an admin approval.
# When disabled, a file that passes the automated security scan can deploy
# immediately; admins still receive a copy/record for moderation.
APPROVAL_REQUIRED = True

# Stop/crash retention. A bot that remains non-running for this long is
# automatically deleted by the monitor. Set to 0 to disable.
AUTO_DELETE_OFFLINE_HOURS = 9999

# Two security-policy violations => hosting ban for a normal user.
MAX_SECURITY_WARNINGS = 2

# Broadcast safety: only admins can use /broadcast or the Admin Panel button.
BROADCAST_BATCH_SIZE = 25
BROADCAST_DELAY_SECONDS = 0.15


# ─── Concurrency / Multi-user stability (v4.1) ──────────────────────────
# How many Telegram updates (button taps / messages) the panel may process
# at the SAME time. Default PTB behaviour is 1 — i.e. while one user's
# upload/deploy was running, every other user got no reply at all.
CONCURRENT_UPDATES = 256
# How many heavy deploys (pip/npm install + start) may run in parallel.
# Others wait in a queue instead of starving the whole server of CPU/RAM.
MAX_PARALLEL_DEPLOYS = 3
# Per-bot python dependencies are installed into DEPS_DIR/<bot_id> instead
# of the panel's own site-packages, so one user's requirements.txt can never
# upgrade/downgrade a library the panel (or another user's bot) depends on.
DEPS_DIR = _os.path.join(BASE_DIR, "deps")
# pip / npm install timeout in seconds
DEPENDENCY_TIMEOUT = 600
# Hosted (process-fallback) bots run with this "nice" value so a busy user
# bot can never starve the panel itself of CPU. 0 = disabled.
HOSTED_PROCESS_NICE = 10

# Per-container process cap (Docker mode only).
CONTAINER_PIDS_LIMIT = 128

# ─── SECURITY (v5.2) ────────────────────────────────────────────────────
# Uploaded/hosted code NEVER runs on the panel host. Ye flags jaan-bujh kar
# False hain — inhe True mat karna.
ALLOW_HOST_INSTALL = False   # panel host par pip/npm install (auto-fix) band
ALLOW_HOST_EXEC = False      # panel host par bot process chalana band

# Har hosted bot isi alag network me chalta hai (bots ek dusre se baat nahi
# kar sakte: enable_icc=false). Subnet harden_host.sh ke firewall rules me bhi
# use hota hai — dono jagah same rakho.
CONTAINER_NETWORK = "hostedbots"
CONTAINER_SUBNET = "172.29.0.0/16"

# Container user (root nahi). "65534:65534" = nobody.
CONTAINER_USER = "65534:65534"

# Optional extra sandbox: agar host par gVisor installed hai to "runsc" likho
# (kernel-escape ka risk bahut kam ho jata hai). None = normal runc.
DOCKER_RUNTIME = None

# Docker build ke time limits (malicious requirements.txt/package.json ke liye)
BUILD_MEMORY_LIMIT = "1g"
CONTAINER_NOFILE_LIMIT = 1024


# ─── SANDBOX (v5.3 — Pterodactyl / Docker-free hosting) ─────────────────
# "auto"    : Docker mile to Docker, warna Landlock sandbox (Pterodactyl panel ke liye)
# "docker"  : sirf Docker      "landlock": sirf Landlock
# Dono na mile => deploy BLOCK (raw host exec kabhi nahi).
SANDBOX_MODE = "auto"

# Landlock sandbox me har bot ki RAM limit (MB). Cross karne par monitor process-tree kill karta hai.
# Pterodactyl server ki total RAM se bahut kam rakho (sab bots + panel share karte hain).
SANDBOX_MEMORY_MB = 2048
# Ek bot ek file kitni badi likh sakta hai (MB)
SANDBOX_MAX_FILE_MB = 200
# True = pip sirf wheels install kare (sdist ka setup.py kabhi na chale). Kuch packages fail honge.
SANDBOX_PIP_ONLY_BINARY = False
# Panel process ko PR_SET_DUMPABLE=0 (same-UID /proc/<pid>/environ read se extra bachav). Landlock
# pehle hi /proc block karta hai, isliye optional.
SANDBOX_HARDEN_PANEL = False


# Fork-bomb guard: ek bot ke process-tree me max processes (monitor har tick check karta hai)
MAX_PROCS_PER_BOT = 40
