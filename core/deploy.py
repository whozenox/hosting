"""
core/deploy.py
Orchestrates: detect runtime -> security scan -> build/run (Docker or
plain process fallback) -> persist bot record in bots.db.
"""

import os
import uuid
import asyncio
import logging
import shutil
from datetime import datetime, timezone
import config
from core import runtime_detector, security, docker_manager, process_manager, dependency_manager, limits, sandbox, crypto_store
from database.db import bots_db, settings_db


logger = logging.getLogger("deploy")


class DeployError(Exception):
    pass


# Heavy work (pip/npm install, docker build) is queued: at most
# config.MAX_PARALLEL_DEPLOYS run at once, the rest wait their turn
# instead of all hammering the CPU/RAM of the server together.
_deploy_sem: asyncio.Semaphore | None = None


def _sem() -> asyncio.Semaphore:
    global _deploy_sem
    if _deploy_sem is None:
        _deploy_sem = asyncio.Semaphore(max(1, int(getattr(config, "MAX_PARALLEL_DEPLOYS", 3))))
    return _deploy_sem


async def deploy_bot_async(*args, **kwargs) -> dict:
    async with _sem():
        return await asyncio.to_thread(deploy_bot, *args, **kwargs)


async def restart_bot_async(*args, **kwargs):
    async with _sem():
        return await asyncio.to_thread(restart_bot, *args, **kwargs)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _docker_ready() -> bool:
    return config.USE_DOCKER and docker_manager.is_docker_available()


def runtime_mode() -> str | None:
    """Kaunsa ISOLATED runtime use hoga: 'docker' | 'sandbox' | None (kuch nahi => deploy block).

    SANDBOX_MODE = "auto"    : Docker ho to Docker, nahi to Landlock (Pterodactyl)
                   "docker"  : sirf Docker
                   "landlock": sirf Landlock
    """
    pref = str(getattr(config, "SANDBOX_MODE", "auto")).lower()
    if pref in ("auto", "docker") and _docker_ready():
        return "docker"
    if pref in ("auto", "landlock") and sandbox.available():
        return "sandbox"
    return None


def no_runtime_message() -> str:
    return (
        "SECURITY BLOCK: no isolated runtime available (Docker ❌, Landlock ❌). "
        "Uploaded code is never run directly on the host.\n"
        f"Landlock: {sandbox.unavailable_reason()}\n"
        "Fix: host ka kernel >= 5.13 + Landlock enabled ho, ya Docker-wala VPS use karo."
    )


def remove_existing(owner_id: int, bot_name: str):
    """Stops and forgets a previously deployed bot with the same
    (owner, name) WITHOUT touching its project folder. bots.db has
    UNIQUE(owner_id, name): re-uploading a bot under the same name used to
    crash with IntegrityError, leaving the user with no reply at all."""
    with bots_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM bots WHERE owner_id=? AND name=?", (owner_id, bot_name)
        ).fetchall()]
    for row in rows:
        try:
            _stop_runtime(row)
            if row.get("container_id"):
                docker_manager.remove_container(row["container_id"])
            if row.get("image_tag"):
                docker_manager.remove_image(row["image_tag"])
        except Exception:
            logger.exception("cleanup of old bot_id=%s failed", row["bot_id"])
        dependency_manager.remove_deps(row["bot_id"])
        with bots_db() as conn:
            conn.execute("DELETE FROM bots WHERE bot_id=?", (row["bot_id"],))


def deploy_bot(owner_id: int, bot_name: str, project_path: str, source: str = "upload", repo_url: str | None = None, skip_scan: bool = False) -> dict:
    """
    Deploys (builds + starts) a bot project that already lives on disk
    at `project_path`. Returns the bots.db row as a dict.
    """
    # Static scanning is a useful filter, NOT a sandbox. Scan every project,
    # including owner/admin uploads; never trust a caller-provided skip_scan flag.
    ok, reason = security.scan_project_tree(project_path)   # also strips symlinks + checks manifests
    if not ok:
        raise DeployError(reason)

    # Fail closed: koi isolated runtime (Docker ya Landlock sandbox) nahi => deploy nahi.
    mode = runtime_mode()
    if mode is None:
        raise DeployError(no_runtime_message())

    try:
        info = runtime_detector.detect_runtime(project_path)
    except runtime_detector.NodeEntrypointAmbiguous as e:
        raise DeployError(str(e))
    runtime = info["runtime"]
    entrypoint = info["entrypoint"]

    if runtime is None or entrypoint is None:
        raise DeployError(
            "Couldn't detect a runnable entrypoint. Make sure your project has a "
            "main.py/bot.py (Python) or package.json + index.js (Node)."
        )

    remove_existing(owner_id, bot_name)

    # Persist a placeholder bot row first so we have a bot_id
    with bots_db() as conn:
        cur = conn.execute(
            """INSERT INTO bots (owner_id, name, source, repo_url, runtime, entrypoint, path, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'deploying')""",
            (owner_id, bot_name, source, repo_url, runtime, entrypoint, project_path),
        )
        bot_id = cur.lastrowid

    try:
        if mode == "docker":
            if not _docker_ready():
                raise DeployError("Docker isolation became unavailable. Deployment cancelled.")
            _deploy_with_docker(bot_id, project_path, runtime, entrypoint, owner_id, bot_name)
        else:
            _deploy_with_process(bot_id, project_path, runtime, entrypoint)
    except Exception as e:
        # A deploy that never got running shouldn't leave a ghost row
        # behind (it kept occupying a hosting slot, and blocked the user
        # from retrying under the same name).
        with bots_db() as conn:
            conn.execute("DELETE FROM bots WHERE bot_id=?", (bot_id,))
        dependency_manager.remove_deps(bot_id)
        msg = str(e)
        raise DeployError(msg[-3000:] if len(msg) > 3000 else msg)

    with bots_db() as conn:
        row = conn.execute("SELECT * FROM bots WHERE bot_id=?", (bot_id,)).fetchone()
        return dict(row)


def _deploy_with_docker(bot_id, project_path, runtime, entrypoint, owner_id, bot_name):
    # Remember whatever image previously backed this bot (if any) so it
    # can be removed once the new one is safely running — otherwise every
    # rebuild (auto-fix, crash-loop auto-restart, manual rebuild) leaves
    # the old image on disk forever and storage fills up within hours.
    with bots_db() as conn:
        row = conn.execute("SELECT image_tag FROM bots WHERE bot_id=?", (bot_id,)).fetchone()
    old_image_tag = row["image_tag"] if row else None

    docker_manager.write_dockerfile(project_path, runtime, entrypoint)
    image_tag = f"hostedbot-{bot_id}-{uuid.uuid4().hex[:6]}".lower()
    container_name = f"hostedbot-{owner_id}-{bot_name}".lower().replace(" ", "-")

    docker_manager.build_image(project_path, image_tag)
    env_vars = _load_env_vars(bot_id)
    cpu_limit = limits.get_cpu_limit(owner_id, is_admin=owner_id in config.ADMIN_IDS)
    container_id = docker_manager.run_container(image_tag, container_name, env_vars, cpu_limit=cpu_limit)

    with bots_db() as conn:
        conn.execute(
            "UPDATE bots SET container_id=?, image_tag=?, status='running', started_at=?, stopped_at=NULL WHERE bot_id=?",
            (container_id, image_tag, _now_iso(), bot_id),
        )

    # New container is up — now it's safe to drop the old image.
    if old_image_tag and old_image_tag != image_tag:
        docker_manager.remove_image(old_image_tag)
    docker_manager.prune_dangling_images()


def _deploy_with_process(bot_id, project_path, runtime, entrypoint):
    """Landlock-sandboxed host process (Docker-free). Install + run dono sandbox ke andar."""
    if not sandbox.available():
        raise DeployError(no_runtime_message())
    sandbox.assert_project_path(project_path)

    # Symlink/manifest dubara check: install se theek pehle.
    ok, reason = security.scan_project_tree(project_path)
    if not ok:
        raise DeployError(reason)

    if runtime == "python":
        ok, out = dependency_manager.install_python_deps(project_path, bot_id=bot_id)
    else:
        ok, out = dependency_manager.install_node_deps(project_path, bot_id=bot_id)
    if not ok:
        raise DeployError(f"Dependency install failed:\n{out}")

    env_vars = _load_env_vars(bot_id)
    try:
        pid = process_manager.start_process(
            project_path, runtime, entrypoint, bot_id, env_vars,
            extra_pythonpath=dependency_manager.deps_dir(bot_id, project_path),
        )
    except RuntimeError as e:
        raise DeployError(str(e))

    with bots_db() as conn:
        conn.execute(
            "UPDATE bots SET pid=?, container_id=NULL, status='running', started_at=?, stopped_at=NULL WHERE bot_id=?",
            (pid, _now_iso(), bot_id),
        )


def _load_env_vars(bot_id: int) -> dict:
    with settings_db() as conn:
        rows = conn.execute("SELECT key, value FROM bot_env WHERE bot_id=?", (bot_id,)).fetchall()
    return {r["key"]: crypto_store.decrypt(r["value"]) for r in rows}


def _stop_runtime(bot_row: dict):
    if bot_row.get("container_id"):
        docker_manager.stop_container(bot_row["container_id"])
    elif bot_row.get("pid"):
        process_manager.stop_process(bot_row["pid"], bot_row.get("entrypoint"))


def stop_bot(bot_row: dict):
    _stop_runtime(bot_row)
    with bots_db() as conn:
        conn.execute("UPDATE bots SET status='stopped', started_at=NULL, stopped_at=? WHERE bot_id=?", (_now_iso(), bot_row["bot_id"]))


def restart_bot(bot_row: dict, force_rebuild: bool = False):
    """
    force_rebuild=True is required whenever the project files on disk
    changed since the container was built (e.g. auto_fix just reinstalled
    node_modules/pip packages) — Docker containers run off a COPY'd
    snapshot taken at build time, not the live uploads/ folder, so a
    plain `docker restart` would just relaunch the same broken image and
    the bot would crash again in an infinite auto-fix loop. Only a real
    rebuild picks up the fix.
    """
    if bot_row.get("container_id"):
        if force_rebuild:
            ok, reason = security.scan_project_tree(bot_row["path"])
            if not ok:
                raise DeployError(reason)
            _deploy_with_docker(
                bot_row["bot_id"], bot_row["path"], bot_row["runtime"],
                bot_row["entrypoint"], bot_row["owner_id"], bot_row["name"],
            )
        else:
            docker_manager.restart_container(bot_row["container_id"])
            with bots_db() as conn:
                conn.execute(
                    "UPDATE bots SET status='running', started_at=?, stopped_at=NULL WHERE bot_id=?",
                    (_now_iso(), bot_row["bot_id"]),
                )
    else:
        # container_id nahi => ya sandbox-process bot hai ya purana legacy row.
        # Dono case me: purana process maaro, dubara scan karo, ISOLATED runtime me hi start karo.
        if bot_row.get("pid"):
            process_manager.stop_process(bot_row["pid"], bot_row.get("entrypoint"))
        ok, reason = security.scan_project_tree(bot_row["path"])
        if not ok:
            raise DeployError(reason)
        mode = runtime_mode()
        if mode is None:
            raise DeployError(no_runtime_message())
        if mode == "docker":
            _deploy_with_docker(
                bot_row["bot_id"], bot_row["path"], bot_row["runtime"],
                bot_row["entrypoint"], bot_row["owner_id"], bot_row["name"],
            )
        else:
            _deploy_with_process(bot_row["bot_id"], bot_row["path"], bot_row["runtime"], bot_row["entrypoint"])
            with bots_db() as conn:
                conn.execute("UPDATE bots SET stopped_at=NULL WHERE bot_id=?", (bot_row["bot_id"],))


def delete_bot(bot_row: dict):
    stop_bot(bot_row)
    if bot_row.get("container_id"):
        docker_manager.remove_container(bot_row["container_id"])
    if bot_row.get("image_tag"):
        docker_manager.remove_image(bot_row["image_tag"])
    shutil.rmtree(bot_row["path"], ignore_errors=True)
    dependency_manager.remove_deps(bot_row["bot_id"])
    try:
        os.remove(os.path.join(config.LOGS_DIR, f"{bot_row['bot_id']}.log"))
    except OSError:
        pass
    with bots_db() as conn:
        conn.execute("DELETE FROM bots WHERE bot_id=?", (bot_row["bot_id"],))
    with settings_db() as conn:
        conn.execute("DELETE FROM bot_env WHERE bot_id=?", (bot_row["bot_id"],))


def get_logs(bot_row: dict, tail: int = None) -> str:
    tail = tail or config.LOG_TAIL_LINES
    if bot_row.get("container_id"):
        try:
            return docker_manager.get_logs(bot_row["container_id"], tail)
        except Exception as e:
            return f"(couldn't fetch container logs: {e})"
    return process_manager.tail_log(bot_row["bot_id"], tail)
