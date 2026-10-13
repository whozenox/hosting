# SECURITY — Hosting Panel v5.2  (Made by @Dev_Null_X)

## Agar data/tokens leak ho chuke hain — PEHLE YE KARO
1. @BotFather → panel ka BOT_TOKEN `/revoke` karke naya token lo.
2. Har hosted bot ke tokens leak maan lo: users ko bolo apne tokens BotFather se revoke karein.
3. Purana server/container DELETE karo aur fresh deploy karo (attacker ka code/backdoor
   `uploads/`, `deps/`, `database/` me reh sakta hai). Purani `uploads/` aur `database/*.db`
   copy mat karo — sirf users ki list chahiye to `users.db` export karke check karo.
4. `bots.db` me dekho kaun sa user/bot suspicious tha, use ban karo.
5. Panel secrets (token) ab env var `BOT_TOKEN` se do, config.py me mat likho.

## Kya fix hua (v5.2)
| Hole | Fix |
|---|---|
| Auto-fix host par `pip/npm install` chalata tha (log se package name) | Host install band. Sirf allow-list package `requirements.txt` me add hota hai, install Docker build ke andar |
| GitHub repo ke symlink se Backup me `config.py` leak | Symlink strip (clone + deploy + restart), backup symlink follow nahi karta |
| Purane (pre-Docker) bots host par restart ho jate the | Ab sirf Docker; legacy rows re-scan hoke container me jate hain |
| `package.json` postinstall / `requirements.txt` custom index | Manifest check + `--ignore-scripts` |
| File browser `startswith` bug | `commonpath` + symlink block |
| Container root, network khula | non-root user, no swap, isolated network (icc off), log cap, ulimits, optional gVisor |
| Scanning/DDoS/spam/metadata | `harden_host.sh` (iptables DOCKER-USER) |
| Infinite auto-restart builds | MAX_AUTO_RESTARTS = 5 |

## Zaroori deployment rules
- **Koyeb/Heroku/Render jaisi jagah par hosting-bot MAT chalao** — wahan Docker socket nahi hota,
  isliye asli sandbox possible hi nahi. Docker wala VPS lo (Hetzner/Contabo/DigitalOcean) aur
  `sudo ./harden_host.sh` chalao.
- Us VPS par apne personal data/keys/SSH keys mat rakho. Sirf hosting ke liye alag VPS.
- Panel ko Docker socket access = host root-equivalent. Best: rootless Docker ya
  `tecnativa/docker-socket-proxy`. Aur bhi safe: `DOCKER_RUNTIME = "runsc"` (gVisor).
- Admin approval (`APPROVAL_REQUIRED = True`) ON rakho aur uploaded code khud dekho.
- Koi bhi system 100% unhackable nahi hota — ye layers risk bahut kam karti hain,
  khatam nahi. Updates + monitoring + backups rakho.

## v5.3 — Pterodactyl / Docker-free sandbox (Landlock)
Pterodactyl me Docker socket nahi hota, isliye ab `SANDBOX_MODE = "auto"`: Docker ho to Docker, nahi to
**Landlock** (Linux kernel sandbox, root nahi chahiye). Dono na mile => deploy BLOCK (fail-closed).

Landlock me hosted bot sirf ye dekh sakta hai: apna project folder, apna deps, apna HOME/tmp, system libs.
Panel ka `config.py`, `.env`, `database/`, `backups/`, doosre users ke folders, `/etc/shadow`, `/proc/<other-pid>`,
`/tmp` => Permission denied. Panel ka BOT_TOKEN child env me nahi jata. pip/npm install bhi sandbox ke andar.
Har bot ki RAM limit `SANDBOX_MEMORY_MB` (monitor enforce karta hai).

Limits (honest): network khula hai (Telegram API ke liye); same container/kernel share hota hai, isliye
kernel-exploit se bachav Docker/gVisor jitna nahi. Isliye `APPROVAL_REQUIRED = True` rakho.
Startup log me dekho: `sandbox: Landlock READY` ya `UNAVAILABLE (reason)`.
