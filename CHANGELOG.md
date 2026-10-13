# v5.2-secure — Security release (Made by @Dev_Null_X)
- FIX (critical): auto-fix no longer runs pip/npm/apt on the panel host; allow-list only, install inside Docker build.
- FIX (critical): symlink file-read (GitHub clone -> backup/file browser) closed; links stripped everywhere.
- FIX: legacy host-process bots are never restarted on the host.
- NEW: manifest checks (npm lifecycle scripts, pip custom index), `--ignore-scripts`.
- NEW: containers run non-root, no swap, isolated network, log caps, optional gVisor runtime.
- NEW: harden_host.sh (blocks metadata/LAN/SMTP, connection limits), SECURITY.md, .dockerignore.
- CHANGE: BOT_TOKEN can come from env var; MAX_AUTO_RESTARTS 1000 -> 5.

# v4.1 — Multi-user stability fixes

**Why the bot stopped answering when several users used it together**
- Updates were processed one at a time (PTB default). One user's upload /
  pip install blocked everybody. Now `concurrent_updates` is on and every
  blocking call (zip scan, extract, git clone, psutil stats, make_archive)
  runs in a worker thread. Heavy deploys are queued (`MAX_PARALLEL_DEPLOYS`).
- Hosted bots' `pip install` went into the panel's own site-packages, so a
  user's requirements could replace the libraries the panel itself uses.
  Each bot now gets an isolated folder (`deps/<bot_id>`, `pip --target`).
- Hosted bots run `nice`d so they can't starve the panel of CPU.

**Other bugs fixed**
- Admin Panel `NameError: maint_style` (button did nothing).
- Crashed hosted bots were never detected (zombie process still "exists").
- After a panel restart all bots were marked crashed and auto-deleted 1h
  later — they are now restarted automatically on boot.
- Re-uploading a bot with the same name crashed (UNIQUE constraint) with no
  reply; failed deploys left ghost rows that occupied slots.
- Slot reservations leaked (approved bots used 2 slots; any error left a
  reservation behind) -> "Hosting slots full" far too early.
- `MAX_BOTS_PER_USER` / `MAX_BOTS_PER_PREMIUM_USER` were never enforced.
- Temp-file name collisions between users; storage cleanup deleted other
  users' in-progress uploads.
- Force-join made 2 API calls per tap -> cached 5 min.
- Admin broadcast of files/photos was swallowed by the upload handler.
- `safe_edit` failed on texts longer than 1024 (caption) / 4096 chars.
- SQLite: WAL + 30s busy timeout (no more "database is locked").
- Bot token no longer printed in logs (httpx logger silenced).

# v4.0 — Hosting Safety & Capacity Upgrade

- Global 50 hosting slots; warning at 30 slots.
- Owner/admin bypass the slot cap.
- Persistent Approval ON/OFF control in Admin Panel.
- Admin receives a copy/review archive for user-hosted files.
- Two security-policy warnings automatically ban normal users from hosting.
- Admin moderation: ban/unban users.
- Admin can start/stop/delete any hosted bot.
- Bots offline/stopped/crashed for 1 hour are automatically deleted.
- Admin broadcast mode via panel button or `/broadcast`.

# Changelog

## v3.6 (Manual admin approval before any bot goes live)

### Added: ✅ Approve / ❌ Cancel review step
- Every upload or GitHub deploy that **passes** the automated scan
  (`core/security.py`) no longer runs automatically anymore — it's held
  as a `pending_deploys` row and sent to every admin as a message with
  the actual code attached (the original file for uploads via its
  Telegram `file_id`; a zip of the cloned repo for GitHub deploys) plus
  ✅ Approve / ❌ Cancel buttons (new `core/approval.py`,
  `handlers/approval.py`).
- **Approve** → deploys the bot (same `deploy.deploy_bot()` as before)
  and edits the user's own "awaiting review" message to confirm it's
  live, with runtime/entrypoint.
- **Cancel** → deletes the uploaded/cloned project files and tells the
  user it was rejected. Nothing is left on disk either way.
- A pending row is consumed the instant an admin taps either button, so
  a second admin's tap (or a double-tap) can't deploy or delete twice.
- **Admins bypass this for their own uploads/deploys** — same as they
  already bypassed the automated scan — since there's no one above an
  admin to approve their own code.
- This is a separate layer from the existing automated malware scan:
  the scan still blocks obviously malicious patterns outright (that
  alert/behavior is unchanged); this catches everything the scan
  *doesn't* flag but you still want to eyeball before it runs.

## v3.5 (Self-healing: emergency cleanup before the disk actually fills)

### Added: crash-prevention when storage gets critical
- `core/storage_guard.is_critical()` — checked every ~30s from
  `core/monitor.py`, not just the routine 10-minute cleanup — returns
  True once usage crosses `config.EMERGENCY_STORAGE_PERCENT` (default
  90%) of `MAX_STORAGE_MB`, **or** the real host disk itself (checked via
  `psutil.disk_usage`, in case something outside `uploads/backups/logs/temp`
  — pip cache, OS files — is eating space too).
- `core/storage_guard.emergency_recover()` — runs automatically the
  moment it's critical: deletes **every** backup zip immediately (not
  just ones older than 10 min) and truncates **every** bot's log all the
  way down to `config.EMERGENCY_LOG_MAX_KB` (default 50KB), instead of
  only trimming ones already over the normal cap. Goal: reclaim enough
  disk to keep the whole panel — and everyone's hosted bots — from
  crashing when the disk is about to hit 100%.
- Admins get pinged (`admin_notify.notify_admins_storage_emergency`)
  every time this fires, with before/after usage, so it's visible
  instead of silently deleting people's backups.

## v3.4 (Storage fixes — fit inside a small disk budget, e.g. 200MB on Koyeb)

### Fixed: unbounded disk growth
- **Docker images were never cleaned up.** Every deploy and every
  force-rebuild (auto-fix, every crash-loop auto-restart — up to
  `MAX_AUTO_RESTARTS` times) built a brand-new tagged image and left the
  old one on disk. `bots.image_tag` now tracks the current image per
  bot; `core/deploy.py` removes the previous image right after the new
  container is up, and `delete_bot()` removes a bot's image too.
  `core/docker_manager.py` gained `remove_image()` / `prune_dangling_images()`.
- **Backup zips were never deleted.** `handlers/backup.py` built a zip
  in `backups/`, sent it, and left it there forever. It's now deleted
  right after sending (`config.BACKUP_AUTO_DELETE`).
- **Per-bot logs grew forever.** `process_manager.py` opened logs in
  append-forever mode with no cap. Logs are now truncated to
  `config.LOG_MAX_KB` (keeping the newest bytes) on every process start.
- **No total-disk budget existed at all.** New `core/storage_guard.py`
  tracks combined usage of `uploads/ + backups/ + logs/ + temp/` against
  `config.MAX_STORAGE_MB` (default 200), rejects new uploads/clones that
  would exceed it, and runs periodic cleanup (temp files, oversized
  logs, stale backups, dangling Docker images) from `core/monitor.py`
  and once on startup (`bot.py`).
- `config.USE_DOCKER` now defaults to `False` and `config.MAX_UPLOAD_MB`
  dropped from 500 to 30 — both were sized for a host with several GB
  free, not a 200MB budget. Flip `USE_DOCKER` back on only on a host
  with real Docker support and disk to spare.

## v2.5 (Per-user CPU limits)

### Added: admin-controlled CPU limits
- `users.db` now has a `cpu_limit` column (nullable REAL) — an
  admin-set override, in CPU cores, for one specific user's hosted bot
  containers. `NULL` (the default for everyone) means "use the tier
  default" below.
- New tier defaults in `config.py`: `FREE_CPU_LIMIT` (1.0), 
  `PREMIUM_CPU_LIMIT` (2.0), `ADMIN_CPU_LIMIT` (4.0) — how many CPU
  cores a Docker-mode bot container gets, based on whether its owner is
  free, premium, or an admin. `core/limits.py` resolves the effective
  limit for a user: override if set, else their tier default.
- `core/docker_manager.run_container()` now takes a `cpu_limit` param
  instead of always reading the single global
  `config.CONTAINER_CPU_LIMIT`; `core/deploy.py` looks up each bot
  owner's effective limit via `core/limits.py` and passes it in on
  every fresh deploy and every force-rebuild (auto-fix / manual
  restart-with-rebuild).
- New admin commands: `/setcpu <user_id> <cores>` to override a specific
  user's limit (e.g. `/setcpu 123456789 2.5`), `/resetcpu <user_id>` to
  clear it back to their tier default. Both take effect on that user's
  *next* deploy/rebuild — an already-running container keeps its
  existing limit until then, same as how CPU limits have always worked
  here (Docker doesn't let you change `nano_cpus` on a running
  container without recreating it).
- **Admin Panel → All Users** now shows the free/premium/admin CPU
  defaults, each recent user's limit if they have a custom override,
  and a separate "CPU overrides set" list.
- **Account** (regular user's own screen) now shows their current CPU
  limit and whether it's their tier default or a custom override, so
  they can see it without asking an admin.
- CPU limits only apply in Docker mode (`config.USE_DOCKER = True`) —
  the process fallback has no cgroup to enforce a limit on, same
  caveat as the existing memory limit.

## v2.4 (Maintenance mode)

### Added: maintenance mode on/off
- `core/maintenance.py` — a single global maintenance flag stored in
  `settings.db` (survives restarts), with `is_on()` / `set_on()` /
  `toggle()`.
- New toggle button in the **Admin Panel** — "🛠 Maintenance: ON/OFF" —
  colored green when off (tap to turn on) and red when on (tap to turn
  off), flipping instantly with no separate confirm step. The panel
  header also shows the current state.
- `bot.py` gained a `maintenance_gate`, registered in `group=-2` — a
  step *before* even the existing force-join gate — so while
  maintenance is on, every command/button tap from a non-admin is
  stopped with **"🛠 Bot is under maintenance. Please wait, we'll be
  back soon!"** before it reaches any real handler. Admins
  (`config.ADMIN_IDS`) always bypass it, so whoever's fixing things can
  keep testing normally while the bot is "down" for everyone else.

## v2.3 (Admin Panel + hosting notifications)

### Added: in-chat Admin Panel
- A new 👑 **Admin Panel** button now appears at the top of the main menu
  — but *only* for user ids listed in `config.ADMIN_IDS`. Non-admins never
  see the button at all (it isn't added to the menu layout for them), and
  every panel callback re-checks admin status itself, so the panel can't
  be reached even by guessing the `callback_data`.
- The panel (`handlers/admin.py`, `callback_data="admin:*"`) has four
  colored ("rich") buttons:
  - 📊 **VPS Status** — same live CPU/RAM/disk/uptime as `/vpsstatus`.
  - 👑 **Premium List** — same as `/premlist`.
  - 🤖 **All Hosted Bots** — the latest 25 bots across *every* user, each
    line showing the bot id, name, **owner's user id**, source
    (upload/github), runtime and status — so an admin can see at a glance
    who hosted what.
  - 👥 **All Users** — total/premium/admin counts plus the most recently
    seen users.
- `/vpsstatus` and `/premlist` were refactored to share their text
  formatting with the new panel callbacks (`_vps_status_text` /
  `_premium_list_text`) instead of duplicating it — no behavior change
  for the existing commands.

### Added: bot uptime ("online since")
- `bots.db` now has a `started_at` column, stamped every time a bot
  transitions to `status='running'` (fresh deploy, manual restart, or
  auto-restart) and cleared on stop/crash. Older rows without it just
  show `—` instead of erroring.
- **VPS Status** (both `/vpsstatus` and the Admin Panel button) now also
  lists currently-running bots with owner id and how long each has been
  online, plus a running-bot count.
- **Admin Panel → All Hosted Bots** shows an "Online for" line per bot.
- **My Bots → (a bot)** shows the same "Online for" line for the bot's
  own owner, not just admins.

### Added: live "new bot hosted" notification
- `core/admin_notify.py` — the moment any user successfully uploads a
  file or deploys from GitHub, every admin gets pushed a rich-message
  card: the user's **id and username**, the bot name, the **file name**
  (uploads) or **repo URL** (GitHub), runtime, and file size, plus quick
  buttons back to the Admin Panel / VPS Status. Falls back to a plain
  Markdown DM if the rich call fails — same fallback contract as every
  other `core/rich_message.py` call site, so a formatting hiccup here can
  never break the uploader's own deploy flow.
- Wired into both `handlers/upload.py` and `handlers/github.py`, right
  after the deploy succeeds.

## v2.0

### Bug fixes
- **Admin commands now work.** `admin.py` was never registered in `bot.py`,
  so `/vpsstatus`, `/addprem`, `/removeprem` and `/premlist` did nothing.
  Registered it.
- **Auto-restart no longer deadlocks.** The background monitor called
  `deploy.restart_bot` while a `bots.db` write transaction was still open,
  causing `database is locked` and silently killing auto-restart. Writes are
  now short, isolated transactions and the restart runs outside them.
- **Settings toggles are honored.** `notify_on_crash` and `auto_fix_enabled`
  from the Settings menu were written but never read — the monitor always
  notified and always auto-fixed. It now respects both per-user.
- **Premium expiry is timezone-safe.** Replaced deprecated
  `datetime.utcnow()` with timezone-aware datetimes, and made expiry
  comparison tolerant of older naive timestamps (no more aware/naive
  `TypeError`).
- **File-handle leaks fixed** in the file browser and backup sender
  (`open()` results are now closed via `with`), and downloads carry a
  proper filename.

### Config
- `BOT_TOKEN` was only the numeric bot-id half (`8610655917`) — not a usable
  token, so the bot could never authenticate. It's now a clearly-marked
  empty placeholder: paste your **full** `123456789:AAH…` token from
  @BotFather.
- Added `VERSION` (shown on the home screen).

### Small additions
- `/help` now opens the main menu (alias of `/start`).

## v2.1

### Colorful buttons
- Bumped `python-telegram-bot` from `21.6` to `>=22.7,<23` — 21.6 predates
  PTB's support for Telegram's button `style` field (added in PTB 22.7;
  Telegram's Bot API added the field itself in 9.4). Passing `style=` on
  21.6 would raise a `TypeError`, so this bump is required, not optional.
- Colored every actionable inline button:
  - 🟢 green (`success`): create / positive actions — Upload, Deploy from
    GitHub, Start/Restart, Try Auto-Fix, Add/Update env var, Cancel (in
    delete confirmation), and ON-state settings toggles.
  - 🔵 blue (`primary`): main navigation — My Bots, Files, Logs, Env Vars,
    Dashboard, Backup, Settings, Account, per-bot Logs/Stats.
  - 🔴 red (`danger`): destructive / stop actions — Stop, Delete, the
    "Yes, delete" confirm, and OFF-state settings toggles.
  - Back/Cancel-navigation and the Channel/YouTube link buttons were left
    uncolored on purpose — Telegram's own default look is what actually
    reads as "secondary" next to colored actions, so leaving them plain
    keeps the buttons that matter from getting lost in a wall of color.

⚠️ Heads-up: I verified the `style=` usage against PTB's own documented
API and compiled every file, but this sandbox has no network access, so I
could not `pip install python-telegram-bot>=22.7` here and run a live
smoke test against Telegram's servers. Run `pip install -r
requirements.txt --upgrade` in a venv and test `/start` once before
relying on this in production — if PTB 22 changed anything else you
depend on, it'll show up there.

## v2.2

### Fixed: Node.js bots that wouldn't host at all
- `runtime_detector.py` only matched a fixed filename list (`index.js`,
  `main.js`, `app.js`, `bot.js`, `server.js`) or an exact `package.json`
  `"main"` field. Any project with a custom-named entry file — e.g.
  `telegramBot.js`, `whatsapp.js`, extremely common for Telegraf/Baileys
  bots — fell through to `entrypoint=None`, and `deploy.py` rejected it
  with a generic "Couldn't detect a runnable entrypoint" no matter how
  correct the code was. Detection now also checks `package.json`
  `scripts.start` (e.g. `"start": "node telegramBot.js"`), then falls back
  to the single `.js`/`.mjs`/`.cjs` file at the project root if there's
  exactly one. If it's still ambiguous (multiple candidate files, no
  hints), deploy now fails with a specific message naming the files found
  instead of a dead end.
- **Auto-fix now actually fixes Docker-hosted Node/Python bots.** Docker
  containers run off a `COPY`'d snapshot taken at build time — they don't
  see the live `uploads/` folder. When auto-fix reinstalled `node_modules`
  (or a pip package) on disk and then just called `docker restart`, the
  container came back up on the *old* image, still missing the package,
  and crashed again — an auto-fix loop that always ended in "crashed" no
  matter how many times it "fixed" it. `deploy.restart_bot()` now takes a
  `force_rebuild` flag; both the background monitor and the manual
  "Try Auto-Fix" button pass `True` so a real image rebuild picks up the
  fix. Plain user-initiated restarts (My Bots → Restart) still do a cheap
  `docker restart` since nothing on disk changed.

### Added: rich message home screen
- `core/rich_message.py` — Bot API 10.3 rich-message block builders
  (heading / paragraph / compact table / video / in-message buttons),
  called via a raw HTTPS request so it works even before PTB ships a typed
  wrapper for `sendRichMessage`. Every call site falls back to the
  existing plain video+caption+keyboard flow if the rich call fails, so a
  field-name mismatch can't break `/start`.
- `/start` and the "⬅️ Back" button now render as a card — heading, a
  compact info table (owner, version, your bot count, Docker vs. process
  fallback), and the menu buttons embedded directly in the message —
  matching the Node.js sibling bot's start screen.

⚠️ Same caveat as v2.1: no network access in this sandbox, so the
`sendRichMessage`/`editMessageText(rich_message=...)` calls are
syntax-checked and shaped against Telegram's documented Bot API 10.3
schema, but not live-tested. If Telegram's actual field names differ, the
fallback keeps `/start` working — but please test once before rolling out.

## V5.1 — Security hardening
- Removed exposed bot token from config; placeholder only.
- Mandatory Docker isolation; no unsafe host-process fallback.
- Always scan every project regardless of owner/admin.
- Hardened Docker runtime defaults and ZIP extraction.
- Fail closed when a source file cannot be inspected.
