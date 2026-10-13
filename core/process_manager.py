"""
core/process_manager.py
Fallback host-process based runner, used automatically when Docker is
unavailable (config.USE_DOCKER = False, or the daemon can't be reached).
Each hosted bot runs as a plain subprocess with output redirected to a
log file under logs/<bot_id>.log.

v4.1 fixes:
  - Crashed bots were NEVER detected: a dead child that nobody wait()s
    for stays as a <defunct> zombie and psutil.pid_exists() keeps
    returning True, so the monitor thought it was still running forever.
    We now keep the Popen handles (poll() reaps the child) and treat
    zombies as dead.
  - After a panel restart an old PID can belong to a totally different
    process. stop_process()/is_running() now verify the command line
    before trusting a stored PID, and never signal the panel itself.
  - stop_process() escalates SIGTERM -> SIGKILL instead of leaving a
    stuck bot alive.
  - Hosted python runs unbuffered (logs used to stay empty until the
    buffer flushed) with the interpreter that runs the panel, and with
    its own per-bot dependency folder first on PYTHONPATH.
  - Hosted bots are `nice`d so they cannot starve the panel of CPU.
"""

import os
import sys
import time
import shutil
import threading
import subprocess
import signal
import psutil
import config
from core import sandbox

RUN_COMMANDS = {
    "python": lambda entry: [sys.executable, "-u", entry],
    "node": lambda entry: ["node", entry],
}

REQUIRED_BINARY = {"python": sys.executable, "node": "node"}

# pid -> Popen for every process this panel instance started.
_PROCS: dict[int, subprocess.Popen] = {}
_LOCK = threading.Lock()


def _cap_log_size(log_path: str):
    """Truncates an existing log file back to config.LOG_MAX_KB (keeping
    the newest bytes) before we start appending to it again."""
    limit_bytes = getattr(config, "LOG_MAX_KB", 300) * 1024
    try:
        if os.path.exists(log_path) and os.path.getsize(log_path) > limit_bytes:
            with open(log_path, "rb") as f:
                f.seek(-limit_bytes, os.SEEK_END)
                tail = f.read()
            with open(log_path, "wb") as f:
                f.write(tail)
    except OSError:
        pass


def _preexec():
    nice = getattr(config, "HOSTED_PROCESS_NICE", 0)
    if nice:
        try:
            os.nice(nice)
        except OSError:
            pass


def start_process(project_path: str, runtime: str, entrypoint: str, bot_id: int,
                  env_vars: dict | None = None, extra_pythonpath: str | None = None) -> int:
    """Hosted bot ko chalata hai.

    Default (ALLOW_HOST_EXEC=False): Landlock sandbox me, clean env ke saath.
    Sandbox na ho to START NAHI hota (fail-closed) — raw host exec kabhi nahi.
    """
    if runtime not in RUN_COMMANDS:
        raise ValueError(f"Unsupported runtime: {runtime}")

    unsafe_host = bool(getattr(config, "ALLOW_HOST_EXEC", False))
    if not unsafe_host and not sandbox.available():
        raise RuntimeError(
            "SECURITY BLOCK: sandbox unavailable, not running uploaded code on the host. "
            + sandbox.unavailable_reason()
        )

    log_path = os.path.join(config.LOGS_DIR, f"{bot_id}.log")
    _cap_log_size(log_path)

    cmd = RUN_COMMANDS[runtime](entrypoint)

    if unsafe_host:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        if extra_pythonpath and runtime == "python" and os.path.isdir(extra_pythonpath):
            existing = env.get("PYTHONPATH")
            env["PYTHONPATH"] = extra_pythonpath + (os.pathsep + existing if existing else "")
        env.update({str(k): str(v) for k, v in (env_vars or {}).items()})
        run_cmd = cmd
    else:
        pp = extra_pythonpath if (runtime == "python" and extra_pythonpath) else None
        env = sandbox.clean_env(bot_id, env_vars, pythonpath=pp)
        binary = REQUIRED_BINARY[runtime]
        if shutil.which(binary, path=env["PATH"]) is None and not os.path.exists(binary):
            raise RuntimeError(f"'{binary}' isn't installed on this server.")
        run_cmd = sandbox.build_command(
            cmd, project_path=project_path, bot_id=bot_id,
            deps_path=extra_pythonpath if runtime == "python" else None,
        )

    with open(log_path, "ab") as log_file:
        proc = subprocess.Popen(
            run_cmd,
            cwd=project_path,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,  # own process group -> clean kill later
            preexec_fn=_preexec if hasattr(os, "nice") else None,
        )
    with _LOCK:
        _PROCS[proc.pid] = proc

    # Sandbox apply fail hua => launcher turant exit 97 deta hai. Ghost "running" row mat banao.
    deadline = time.time() + 1.5
    while time.time() < deadline:
        rc = proc.poll()
        if rc is not None:
            with _LOCK:
                _PROCS.pop(proc.pid, None)
            if rc == sandbox_exit_code():
                raise RuntimeError("SECURITY BLOCK: sandbox apply failed.\n" + tail_log(bot_id, 8))
            break
        time.sleep(0.1)
    return proc.pid


def sandbox_exit_code() -> int:
    return 97


def memory_limit_mb() -> int:
    return int(getattr(config, "SANDBOX_MEMORY_MB", 512))


def enforce_memory(pid: int, bot_id: int | None = None) -> bool:
    """True = limit ke andar. Limit cross hone par poora process-tree kill (panel OOM se bachao)."""
    limit = memory_limit_mb()
    if not limit or not pid:
        return True
    try:
        p = psutil.Process(pid)
        rss = p.memory_info().rss
        for c in p.children(recursive=True):
            try:
                rss += c.memory_info().rss
            except psutil.NoSuchProcess:
                pass
    except psutil.NoSuchProcess:
        return True
    if rss / 1024 / 1024 <= limit:
        return True
    if bot_id is not None:
        try:
            with open(os.path.join(config.LOGS_DIR, f"{bot_id}.log"), "ab") as f:
                f.write(f"\n[host] killed: memory limit exceeded ({rss // 1024 // 1024}MB > {limit}MB)\n".encode())
        except OSError:
            pass
    stop_process(pid, None, grace_seconds=2)
    return False


def _cmdline_matches(pid: int, entrypoint: str | None) -> bool:
    """True if `pid` looks like one of OUR hosted processes (its command
    line mentions the bot's entry file). Protects against PID reuse."""
    if not entrypoint:
        return True
    try:
        cmd = psutil.Process(pid).cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    base = os.path.basename(entrypoint)
    return any(base in part for part in cmd)


def is_running(pid: int, entrypoint: str | None = None) -> bool:
    if not pid:
        return False
    with _LOCK:
        proc = _PROCS.get(pid)
    if proc is not None:
        if proc.poll() is None:      # poll() also reaps the zombie
            return True
        with _LOCK:
            _PROCS.pop(pid, None)
        return False

    # Not started by this panel instance (e.g. survived a panel restart).
    try:
        p = psutil.Process(pid)
        if p.status() == psutil.STATUS_ZOMBIE:
            return False
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        pass
    return _cmdline_matches(pid, entrypoint)


def stop_process(pid: int, entrypoint: str | None = None, grace_seconds: float = 5.0):
    """SIGTERM the bot's whole process group, wait, then SIGKILL if needed."""
    if not pid or pid == os.getpid():
        return
    if not is_running(pid, entrypoint):
        return

    def _signal(sig):
        try:
            pgid = os.getpgid(pid)
            # Never signal our own group (would take the panel down).
            if pgid != os.getpgrp():
                os.killpg(pgid, sig)
            else:
                os.kill(pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            try:
                psutil.Process(pid).send_signal(sig)
            except psutil.NoSuchProcess:
                pass

    _signal(signal.SIGTERM)
    deadline = time.time() + grace_seconds
    while time.time() < deadline:
        if not is_running(pid, entrypoint):
            return
        time.sleep(0.2)
    _signal(signal.SIGKILL)
    time.sleep(0.3)
    is_running(pid, entrypoint)  # reap


def get_stats(pid: int, entrypoint: str | None = None) -> dict:
    try:
        if not is_running(pid, entrypoint):
            return {"status": "stopped", "cpu_percent": 0, "mem_usage_mb": 0}
        p = psutil.Process(pid)
        with p.oneshot():
            return {
                "status": p.status(),
                "cpu_percent": p.cpu_percent(interval=0.2),
                "mem_usage_mb": round(p.memory_info().rss / 1024 / 1024, 1),
            }
    except psutil.NoSuchProcess:
        return {"status": "stopped", "cpu_percent": 0, "mem_usage_mb": 0}


def tail_log(bot_id: int, lines: int = 100) -> str:
    log_path = os.path.join(config.LOGS_DIR, f"{bot_id}.log")
    if not os.path.exists(log_path):
        return "(no logs yet)"
    # Only read the end of the file — logs can be large.
    with open(log_path, "rb") as f:
        try:
            f.seek(-200_000, os.SEEK_END)
        except OSError:
            f.seek(0)
        content = f.read().decode(errors="replace").splitlines()
    return "\n".join(content[-lines:]) or "(no logs yet)"
