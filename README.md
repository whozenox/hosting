# Telegram Hosting Bot  (v2.0)

A Telegram bot that lets your users deploy and manage their own bots/apps
(Python or Node.js) straight from Telegram — upload a file or paste a
GitHub link, and it goes live in a container (or a plain process if
Docker isn't available).

## Features

- ⬆️ Upload a `.zip` / `.py` / `.js` project and deploy it
- 🐙 Deploy directly from a public GitHub repo
- 🤖 Manage hosted bots: start / stop / restart / delete
- 📁 Browse and download files from a hosted bot
- 📜 View logs, with a one-tap **Auto-Fix** for common errors (missing
  dependency, etc.)
- 🔑 Per-bot environment variables
- 📊 Live CPU / RAM dashboard
- 💾 One-click zip backups
- ⚙️ Per-user settings (crash notifications, auto-fix toggle)
- 👑 Owner/admin credits shown on every message

## Requirements

- Ubuntu 22.04 or 24.04 (or any modern Linux)
- Python 3.10+
- Docker Engine (recommended, for real sandboxing). If Docker isn't
  installed/running, the bot automatically falls back to hosting bots
  as plain subprocesses — fine for testing, **not** recommended for
  untrusted users in production. In fallback mode, the *host* itself
  needs `python3`/`pip` and `node`/`npm` installed for those runtimes
  to work: `sudo apt install -y python3-pip nodejs npm`.

## Installation

```bash
# 1. Clone / copy this project onto your server
cd telegram-hosting-bot

# 2. Create a virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. (Recommended) Install Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# log out/in (or `newgrp docker`) so the bot process can use Docker
# without sudo

# 5. Configure the bot
nano config.py
#   - Set BOT_TOKEN to your token from @BotFather
#   - Set OWNER_ID and ADMIN_IDS to your Telegram numeric user ID
#     (use @userinfobot to find it)

# 6. Run it
python3 bot.py
```

The bot creates its own `database/`, `uploads/`, `containers/`,
`backups/`, `logs/` and `temp/` folders and SQLite files on first run —
you don't need to create them manually.

## Running as a service (optional)

Create `/etc/systemd/system/telegram-hosting-bot.service`:

```ini
[Unit]
Description=Telegram Hosting Bot
After=network.target docker.service

[Service]
Type=simple
WorkingDirectory=/path/to/telegram-hosting-bot
ExecStart=/path/to/telegram-hosting-bot/venv/bin/python3 bot.py
Restart=always
RestartSec=5
User=youruser

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-hosting-bot
```

## Configuration reference (`config.py`)

| Setting | Description |
|---|---|
| `BOT_TOKEN` | Your bot's token from @BotFather |
| `OWNER_ID` / `ADMIN_IDS` | Numeric Telegram user IDs with admin rights |
| `USE_DOCKER` | `True` to sandbox bots in Docker, `False` to always use plain processes |
| `MAX_UPLOAD_MB` | Max upload size accepted from users |
| `MAX_BOTS_PER_USER` | Per-user hosting limit (admins are unlimited) |
| `CONTAINER_CPU_LIMIT` / `CONTAINER_MEMORY_LIMIT` | Per-bot resource caps (Docker mode only) |
| `LOG_TAIL_LINES` | Lines shown by default in the Logs menu |
| `MAX_AUTO_RESTARTS` | How many times the monitor auto-restarts a crashed bot before giving up |

All credit/branding strings (`CREDIT_OWNER`, `CREDIT_TELEGRAM_CHANNEL`,
`CREDIT_YOUTUBE_CHANNEL`, `CREDIT_HOME_VIDEO_URL`, `CREDIT_FOOTER`) are
also in `config.py` and appended to every bot message automatically.

## Project structure

```
telegram-hosting-bot/
├── bot.py                 # entry point, handler registration, text router
├── config.py               # all configuration (no .env)
├── requirements.txt
├── handlers/                # one module per menu section
│   ├── home.py               # /start + main menu
│   ├── upload.py              # file upload -> deploy
│   ├── github.py              # repo URL -> clone -> deploy
│   ├── mybots.py               # start/stop/restart/delete
│   ├── files.py                 # file browser
│   ├── logs.py                   # log viewer + auto-fix trigger
│   ├── env.py                     # per-bot env vars
│   ├── dashboard.py                 # CPU/RAM overview
│   ├── backup.py                     # zip + send backup
│   ├── settings.py                    # user preferences
│   └── account.py                      # account info
├── core/                     # business logic, no Telegram imports
│   ├── deploy.py               # orchestrates a deploy end-to-end
│   ├── docker_manager.py        # build/run/stop containers
│   ├── process_manager.py        # subprocess fallback (no Docker)
│   ├── dependency_manager.py      # pip/npm installs
│   ├── runtime_detector.py         # python vs node + entrypoint
│   ├── auto_fix.py                  # pattern-match common log errors
│   ├── security.py                   # upload size/type checks, limits
│   ├── github_manager.py              # git clone wrapper
│   └── monitor.py                      # background crash watcher
├── database/                 # SQLite files, created at runtime
│   ├── db.py                   # connection helpers + schema
│   ├── users.db
│   ├── bots.db
│   └── settings.db
├── uploads/    # per-user project source
├── containers/ # scratch space for container builds
├── backups/    # generated zip backups
├── logs/       # per-bot logs (process fallback mode)
└── temp/       # transient upload staging
```

## Security notes

- Docker is the real isolation boundary here — the `security.py`
  checks (file extension/size limits) are a first line of defense,
  not a sandbox by themselves. Don't run in process-fallback mode
  (`USE_DOCKER = False`) with untrusted users.
- Consider adding a firewall/egress policy on your Docker network if
  hosted bots shouldn't be able to reach your internal network.
- Back up `database/` regularly — it's the only record of who owns
  which bot.


## Security hardening (V5.1)

- **Rotate the Telegram bot token immediately** if the previous `config.py` was shared/uploaded publicly. The token in this archive is a placeholder; create a fresh token with BotFather and insert it locally.
- Untrusted uploads are always statically scanned, including owner/admin uploads. Static scanning is only a filter and cannot prove a project is safe.
- The panel fails closed when Docker isolation is unavailable. It will **not** run user uploads as ordinary processes on the panel host.
- Docker containers drop Linux capabilities, use `no-new-privileges`, have a PID limit, a read-only root filesystem, and a restricted temporary filesystem.
- ZIP extraction rejects path traversal, symlinks, and excessive expansion.

### Important limitation

No static scanner can guarantee that every malicious program will be detected, and no hosting system can honestly promise that an AI or attacker can never bypass security. For a production multi-tenant host, use a dedicated VM/worker per customer, patched Docker/container runtime, separate credentials, outbound network policy, monitoring, and backups. Never keep cloud/VPS credentials or the panel's bot token inside folders accessible to hosted projects.
