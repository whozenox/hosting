"""
core/sandbox.py  —  Docker-free sandbox (Landlock) for Pterodactyl / shared hosts

Panel ke andar se hosted bot ko is tarah chalata hai ki wo SIRF:
  - apna project folder      (read/write/exec)
  - apna deps folder         (read/exec)
  - apna HOME/tmp            (read/write)
  - system libs (/usr, /lib, /etc, python/node install)  (read-only)
dekh sake. Panel ka config.py/.env/database/backups/doosre users ke folders => EACCES.

Extra: panel ka BOT_TOKEN/env child ko NAHI jata (clean env), /proc sirf apna
(/proc/self) khula hai taaki doosre bots ka environ na padh sake.

Limits (Landlock ABI<4): network khula rehta hai (bots ko Telegram API chahiye).
Isliye 127.0.0.1 ke panel-local services ko firewall/auth se alag protect karo.
"""

import os
import sys
import json
import base64
import shutil
import site
import logging
import subprocess
import threading
import config

logger = logging.getLogger("sandbox")

_LAUNCHER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sandbox_exec.py")
_probe_cache: tuple[bool, str] | None = None
_probe_lock = threading.Lock()


# ─── availability ─────────────────────────────────────────────────────
def probe(force: bool = False) -> tuple[bool, str]:
    """(ok, detail). Alag subprocess me test hota hai taaki panel khud restrict na ho."""
    global _probe_cache
    with _probe_lock:
        if _probe_cache is not None and not force:
            return _probe_cache
        try:
            r = subprocess.run([sys.executable, "-I", _LAUNCHER, "--probe"],
                               capture_output=True, text=True, timeout=20,
                               stdin=subprocess.DEVNULL)
            out = (r.stdout + r.stderr).strip()
            _probe_cache = (r.returncode == 0, out or f"exit {r.returncode}")
        except Exception as e:
            _probe_cache = (False, f"probe error: {e}")
        if _probe_cache[0]:
            logger.info("Landlock sandbox ready (%s)", _probe_cache[1])
        else:
            logger.warning("Landlock sandbox NOT available: %s", _probe_cache[1])
        return _probe_cache


def available() -> bool:
    return probe()[0]


def unavailable_reason() -> str:
    return probe()[1]


# ─── path safety ──────────────────────────────────────────────────────
def _real(p: str) -> str:
    return os.path.realpath(p)


def _is_within(child: str, parent: str) -> bool:
    child, parent = _real(child), _real(parent)
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:
        return False


def _protected_paths() -> list[str]:
    names = ("DATABASE_DIR", "BACKUPS_DIR", "LOGS_DIR", "TEMP_DIR", "UPLOADS_DIR", "CONTAINERS_DIR")
    out = [getattr(config, n) for n in names if getattr(config, n, None)]
    out += [config.BASE_DIR, os.path.join(config.BASE_DIR, "config.py"),
            os.path.join(config.BASE_DIR, ".env")]
    return [_real(p) for p in out]


def _safe_grant(path: str) -> bool:
    """Koi bhi shared grant (system/python/node) panel ke secrets ko cover na kare."""
    rp = _real(path)
    for prot in _protected_paths():
        if _is_within(prot, rp):          # grant protected path ka ancestor/equal hai
            return False
        if _is_within(rp, prot) and prot != _real(config.BASE_DIR):
            return False                  # grant protected folder ke ANDAR hai
    return True


def assert_project_path(project_path: str) -> str:
    """Project uploads/<user>/<bot> ke andar hi hona chahiye (uploads root nahi)."""
    rp = _real(project_path)
    up = _real(config.UPLOADS_DIR)
    if not _is_within(rp, up) or rp == up:
        raise RuntimeError("SECURITY BLOCK: project path uploads/ ke bahar hai.")
    if os.path.dirname(rp) == up:
        raise RuntimeError("SECURITY BLOCK: project path user folder ke upar hai.")
    return rp


# ─── policy builders ──────────────────────────────────────────────────
_SYSTEM_RX = ["/usr", "/lib", "/lib32", "/lib64", "/bin", "/sbin"]
# /etc POORA nahi — shadow/sudoers/ssh keys jaisi cheezein bahar rahen
_SYSTEM_RO = [
    "/etc/ssl", "/etc/pki", "/etc/ca-certificates", "/etc/ca-certificates.conf",
    "/etc/resolv.conf", "/etc/hosts", "/etc/host.conf", "/etc/nsswitch.conf", "/etc/gai.conf",
    "/etc/passwd", "/etc/group", "/etc/services", "/etc/protocols",
    "/etc/localtime", "/etc/timezone", "/etc/os-release", "/etc/mime.types",
    "/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/ld.so.conf.d", "/etc/alternatives",
    "/etc/python3", "/etc/python3.10", "/etc/python3.11", "/etc/python3.12", "/etc/python3.13",
    "/etc/fonts", "/etc/locale.alias", "/usr/share/zoneinfo",
    # pip (vendored `distro`) distro-info files padhta hai User-Agent banane ke liye.
    # Inke bina pip: PermissionError '/etc/debian_version' => har install fail.
    "/etc/debian_version", "/etc/lsb-release", "/etc/issue", "/etc/issue.net",
    "/etc/redhat-release", "/etc/centos-release", "/etc/fedora-release",
    "/etc/alpine-release", "/etc/system-release", "/etc/arch-release",
]
_PROC_FILES = ["/proc/cpuinfo", "/proc/meminfo", "/proc/uptime", "/proc/loadavg",
               "/proc/version", "/proc/stat", "/proc/sys/kernel/random"]
_DEV_RW = ["/dev/null", "/dev/zero", "/dev/full", "/dev/tty"]
_DEV_RO = ["/dev/urandom", "/dev/random"]


def _interpreter_paths() -> list[str]:
    cands = {sys.prefix, sys.base_prefix, sys.exec_prefix,
             os.path.dirname(_real(sys.executable))}
    try:
        cands.update(site.getsitepackages())
    except Exception:
        pass
    try:
        cands.add(site.getusersitepackages())
    except Exception:
        pass
    return [c for c in cands if c and os.path.exists(c)]


def _binary_prefixes(name: str) -> list[str]:
    exe = shutil.which(name)
    if not exe:
        return []
    real = _real(exe)
    return [os.path.dirname(os.path.dirname(real)), os.path.dirname(real)]


def _shared_rx(extra_bins: tuple[str, ...] = ("node", "npm", "ffmpeg")) -> list[str]:
    paths = list(_SYSTEM_RX) + _interpreter_paths()
    deps_root = getattr(config, "DEPS_DIR", os.path.join(config.BASE_DIR, "deps"))
    for sub in ("_bin", "_tools"):
        paths.append(os.path.join(deps_root, sub))
    for b in extra_bins:
        pref = _binary_prefixes(b)
        for cand in pref:                      # pehle grandparent, phir parent
            if _safe_grant(cand):
                paths.append(cand)
                break
    safe, seen = [], set()
    for p in paths:
        rp = _real(p)
        if rp in seen or not os.path.exists(rp):
            continue
        if _safe_grant(rp):
            safe.append(rp)
            seen.add(rp)
        else:
            logger.warning("sandbox: unsafe shared grant skipped: %s", p)
    return safe


def bot_home(bot_id: int) -> str:
    d = os.path.join(config.CONTAINERS_DIR, f"bot_{int(bot_id)}")
    os.makedirs(d, exist_ok=True)
    os.makedirs(os.path.join(d, "tmp"), exist_ok=True)
    return d


def clean_env(bot_id: int, extra: dict | None = None, pythonpath: str | None = None) -> dict:
    """Panel ki env (BOT_TOKEN etc.) NAHI. Sirf zaroori + user ke bot_env vars."""
    home = bot_home(bot_id)
    deps_bin = os.path.join(getattr(config, "DEPS_DIR", os.path.join(config.BASE_DIR, "deps")), "_bin")
    env = {
        "PATH": os.pathsep.join([deps_bin, "/usr/local/bin", "/usr/bin", "/bin"]),
        "HOME": home,
        "TMPDIR": os.path.join(home, "tmp"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "TZ": os.environ.get("TZ", "UTC"),
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_NO_CACHE_DIR": "1",
        "npm_config_cache": os.path.join(home, ".npm"),
        "NODE_OPTIONS": f"--max-old-space-size={int(getattr(config, 'SANDBOX_MEMORY_MB', 512) * 0.8)}",
    }
    if pythonpath:
        env["PYTHONPATH"] = pythonpath
    for k, v in (extra or {}).items():
        k = str(k)
        if k.upper() in {"LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONSTARTUP", "PYTHONHOME"}:
            continue
        env[k] = str(v)
    return env


def build_command(cmd: list[str], *, project_path: str, bot_id: int, deps_path: str | None,
                  writable_deps: bool = False, extra_rw: list[str] | None = None) -> list[str]:
    ok, why = probe()
    if not ok:
        raise RuntimeError(
            "SECURITY BLOCK: sandbox unavailable, not running uploaded code on the host. " + why)
    proj = assert_project_path(project_path)
    home = bot_home(bot_id)

    rwx = [proj]
    rw = [home] + [p for p in (extra_rw or []) if _safe_grant(p) or _is_within(p, home)]
    rx = _shared_rx()
    if deps_path:
        os.makedirs(deps_path, exist_ok=True)
        (rwx if writable_deps else rx).append(_real(deps_path))

    # "/proc/@self" launcher me apne PID se replace hota hai (parent ka nahi!)
    proc_self = "/proc/@self"
    policy = {
        "cwd": proj,
        "rx": rx,
        "ro": [p for p in _SYSTEM_RO if os.path.exists(p)] + _PROC_FILES + [proc_self],
        "rw": rw,
        "rwx": rwx,
        "dev": _DEV_RW,
        "devro": _DEV_RO,
        "limits": {
            "nofile": int(getattr(config, "CONTAINER_NOFILE_LIMIT", 1024)),
            "fsize_mb": int(getattr(config, "SANDBOX_MAX_FILE_MB", 200)),
        },
    }
    blob = base64.b64encode(json.dumps(policy).encode()).decode()
    return [sys.executable, "-I", _LAUNCHER, blob, "--", *cmd]


def run_sandboxed(cmd: list[str], *, project_path: str, bot_id: int, deps_path: str | None = None,
                  writable_deps: bool = False, env_extra: dict | None = None,
                  pythonpath: str | None = None, timeout: int = 600) -> tuple[bool, str]:
    """pip/npm install jaisi cheezein bhi sandbox me (setup.py/postinstall host ko na chhu sake)."""
    try:
        full = build_command(cmd, project_path=project_path, bot_id=bot_id,
                             deps_path=deps_path, writable_deps=writable_deps)
    except Exception as e:
        return False, str(e)
    env = clean_env(bot_id, env_extra, pythonpath)
    try:
        r = subprocess.run(full, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, env=env)
        return r.returncode == 0, (r.stdout + r.stderr)[-3000:]
    except subprocess.TimeoutExpired:
        return False, "install timed out."
    except FileNotFoundError as e:
        return False, str(e)


def harden_panel_process():
    """Panel process ko same-UID bots se /proc/<pid>/{environ,mem} padhne se bachao."""
    try:
        import ctypes
        ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)    # PR_SET_DUMPABLE = 0
    except Exception:
        pass
