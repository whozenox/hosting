"""
core/dependency_manager.py
Installs dependencies for the non-Docker (process_manager) fallback path.
When running under Docker, dependency installation happens inside the
image build instead (see docker_manager.py Dockerfile templates).

v4.1: Python packages are NO LONGER installed into the panel's own
site-packages. Every bot gets its own folder (config.DEPS_DIR/<bot_id>)
installed with `pip --target`, and process_manager puts it first on
PYTHONPATH. Before this, a user's requirements.txt pinning e.g.
python-telegram-bot==13 or another httpx version would silently
replace the library the panel itself runs on (and every other user's
bot) — the classic reason "everything stops responding" once several
people host bots at once.
"""

import os
import sys
import shutil
import hashlib
import subprocess
import config
from core import sandbox


def _timeout() -> int:
    return int(getattr(config, "DEPENDENCY_TIMEOUT", 600))


def deps_dir(bot_id: int | None, project_path: str) -> str:
    if bot_id is not None:
        return os.path.join(getattr(config, "DEPS_DIR", os.path.join(config.BASE_DIR, "deps")), str(bot_id))
    return os.path.join(project_path, ".pydeps")


def remove_deps(bot_id: int):
    shutil.rmtree(deps_dir(bot_id, ""), ignore_errors=True)


def _missing_binary_message(binary: str) -> str:
    install_hint = {
        "npm": "Install Node.js + npm: `sudo apt install -y nodejs npm` (Ubuntu), "
               "or switch USE_DOCKER = True in config.py so Node runs inside a container instead.",
        "pip": "Install pip: `sudo apt install -y python3-pip` (Ubuntu), "
               "or switch USE_DOCKER = True in config.py so Python runs inside a container instead.",
    }.get(binary, f"Install `{binary}` on this server.")
    return f"'{binary}' isn't installed on this server. {install_hint}"


def _file_hash(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def _free_mb(path: str) -> float:
    try:
        return shutil.disk_usage(path).free / (1024 * 1024)
    except OSError:
        return float("inf")


def _pip_install(args: list[str], cwd: str, timeout: int) -> tuple[bool, str]:
    # Free disk check first: a half-finished pip run leaves partial files
    # behind and makes the shortage worse.
    need = int(getattr(config, "MIN_FREE_DISK_MB", 400))
    free = _free_mb(config.BASE_DIR)
    if free < need:
        try:
            from core import storage_guard
            storage_guard.emergency_recover()
        except Exception:
            pass
        free = _free_mb(config.BASE_DIR)
        if free < need:
            return False, (f"Not enough disk space ({free:.0f}MB free, need {need}MB). "
                           "Free some space or use a bigger plan/volume.")

    # pip builds/clones into TMPDIR (/tmp by default, often tiny or RAM-backed).
    # Keep it on the panel's own temp folder so it's cleaned by storage_guard.
    tmp = os.path.join(config.BASE_DIR, "temp", "pip")
    os.makedirs(tmp, exist_ok=True)
    env = os.environ.copy()
    env["TMPDIR"] = tmp
    env["PIP_NO_CACHE_DIR"] = "1"

    cmd = [sys.executable, "-m", "pip", "install", "--no-cache-dir",
           "--disable-pip-version-check", *args]
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, env=env,
        )
        shutil.rmtree(tmp, ignore_errors=True)
        return result.returncode == 0, (result.stdout + result.stderr)[-3000:]
    except subprocess.TimeoutExpired:
        return False, "pip install timed out."
    except FileNotFoundError:
        return False, _missing_binary_message("pip")


def _sandbox_install_ok() -> bool:
    return bool(getattr(config, "ALLOW_HOST_INSTALL", False)) or sandbox.available()


def _pip_sandboxed(args: list[str], project_path: str, bot_id: int | None, target: str, timeout: int):
    """pip ko Landlock sandbox me chalao: setup.py sirf bot ke deps folder me likh sakta hai."""
    if bot_id is None:
        return False, "SECURITY BLOCK: bot_id required for sandboxed install."
    need = int(getattr(config, "MIN_FREE_DISK_MB", 400))
    if _free_mb(config.BASE_DIR) < need:
        return False, f"Not enough disk space (need {need}MB free)."
    extra = []
    if getattr(config, "SANDBOX_PIP_ONLY_BINARY", False):
        extra = ["--only-binary=:all:"]
    return sandbox.run_sandboxed(
        [sys.executable, "-m", "pip", "install", "--no-cache-dir", "--disable-pip-version-check", *extra, *args],
        project_path=project_path, bot_id=bot_id, deps_path=target, writable_deps=True,
        timeout=timeout,
    )


def install_python_deps(project_path: str, timeout: int | None = None, bot_id: int | None = None) -> tuple[bool, str]:
    if not _sandbox_install_ok():
        return False, "SECURITY BLOCK: no sandbox available, host install disabled."
    use_sandbox = not getattr(config, "ALLOW_HOST_INSTALL", False)
    req = os.path.join(project_path, "requirements.txt")
    if not os.path.exists(req):
        return True, "No requirements.txt found — skipping."

    target = deps_dir(bot_id, project_path)
    marker = os.path.join(target, ".requirements.sha1")
    try:
        digest = _file_hash(req)
        # Same requirements already installed -> a plain restart doesn't
        # need to hit PyPI again.
        if os.path.exists(marker) and open(marker).read().strip() == digest:
            return True, "Dependencies already installed — skipping."
    except OSError:
        digest = None

    os.makedirs(target, exist_ok=True)
    pip_args = ["--upgrade", "--target", target, "-r", "requirements.txt"]
    if use_sandbox:
        ok, out = _pip_sandboxed(pip_args, project_path, bot_id, target, timeout or _timeout())
    else:
        ok, out = _pip_install(pip_args, project_path, timeout or _timeout())
    if not ok:
        # Don't keep a half-installed folder eating disk.
        shutil.rmtree(target, ignore_errors=True)
    if ok and digest:
        try:
            with open(marker, "w") as f:
                f.write(digest)
        except OSError:
            pass
    return ok, out


def install_node_deps(project_path: str, timeout: int | None = None, bot_id: int | None = None) -> tuple[bool, str]:
    if not _sandbox_install_ok():
        return False, "SECURITY BLOCK: no sandbox available, host install disabled."
    use_sandbox = not getattr(config, "ALLOW_HOST_INSTALL", False)
    pkg = os.path.join(project_path, "package.json")
    if not os.path.exists(pkg):
        return True, "No package.json found — skipping."

    add_bin_to_path()
    if shutil.which("npm") is None:
        return False, _missing_binary_message("npm")

    if use_sandbox:
        if bot_id is None:
            return False, "SECURITY BLOCK: bot_id required for sandboxed install."
        return sandbox.run_sandboxed(
            ["npm", "install", "--omit=dev", "--no-audit", "--no-fund", "--ignore-scripts"],
            project_path=project_path, bot_id=bot_id, timeout=timeout or _timeout())

    try:
        result = subprocess.run(
            ["npm", "install", "--omit=dev", "--no-audit", "--no-fund", "--ignore-scripts"],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=timeout or _timeout(),
            stdin=subprocess.DEVNULL,
        )
        ok = result.returncode == 0
        return ok, (result.stdout + result.stderr)[-3000:]
    except subprocess.TimeoutExpired:
        return False, "npm install timed out."
    except FileNotFoundError:
        return False, _missing_binary_message("npm")


def install_missing_python_package(module_name: str, project_path: str, bot_id: int | None = None) -> tuple[bool, str]:
    """Install one missing import into the bot's isolated dependency folder.

    `module_name` is the Python import name, not necessarily the PyPI name.
    The mapping lives in core.auto_fix so the diagnostic layer decides which
    distribution to install. We also verify the import after installation so
    Auto-Fix never reports a false success.
    """
    if not _sandbox_install_ok():
        return False, "SECURITY BLOCK: no sandbox available, host install disabled."
    use_sandbox = not getattr(config, "ALLOW_HOST_INSTALL", False)
    from core.auto_fix import pip_name_for

    module_name = str(module_name).strip()
    package_name = pip_name_for(module_name)
    target = deps_dir(bot_id, project_path)
    os.makedirs(target, exist_ok=True)

    if use_sandbox:
        ok, out = _pip_sandboxed(["--upgrade", "--target", target, package_name], project_path, bot_id, target, 180)
    else:
        ok, out = _pip_install(["--upgrade", "--target", target, package_name], project_path, 180)
    if not ok:
        return False, out

    # Verify with the same interpreter and the same isolated dependency path
    # used by hosted Python processes. This catches bad package mappings and
    # packages whose installation technically succeeded but don't provide the
    # requested import.
    env = os.environ.copy()
    old_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = target + (os.pathsep + old_pp if old_pp else "")
    check_code = (
        "import importlib.util; "
        f"name={module_name.split('.', 1)[0]!r}; "
        "raise SystemExit(0 if importlib.util.find_spec(name) else 1)"
    )
    try:
        check = subprocess.run(
            [sys.executable, "-c", check_code],
            cwd=project_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return False, f"Installed '{package_name}', but import verification timed out."
    except Exception as e:
        return False, f"Installed '{package_name}', but import verification failed: {e}"

    if check.returncode != 0:
        detail = (check.stdout + check.stderr).strip()[-1500:]
        return False, (
            f"pip installed '{package_name}', but import '{module_name}' is still unavailable."
            + (f"\n{detail}" if detail else "")
        )

    return True, out


def ensure_ffmpeg() -> tuple[bool, str]:
    """Make sure an `ffmpeg` binary exists on PATH (music/video bots need it).

    1. already installed -> done
    2. root + apt-get    -> apt-get install ffmpeg
    3. otherwise         -> static build via pip (static-ffmpeg) put on PATH
    """
    if shutil.which("ffmpeg"):
        return True, "FFmpeg is already installed."

    if shutil.which("apt-get") and hasattr(os, "geteuid") and os.geteuid() == 0:
        try:
            subprocess.run(["apt-get", "update"], capture_output=True, timeout=300,
                           stdin=subprocess.DEVNULL)
            r = subprocess.run(["apt-get", "install", "-y", "--no-install-recommends", "ffmpeg"],
                               capture_output=True, text=True, timeout=900,
                               stdin=subprocess.DEVNULL)
            subprocess.run(["apt-get", "clean"], capture_output=True, timeout=60)
            if r.returncode == 0 and shutil.which("ffmpeg"):
                return True, "Installed FFmpeg with apt. Restarting bot."
        except Exception:
            pass

    # No root (Pterodactyl, shared hosts): pip wheel that bundles a static
    # ffmpeg binary. Works without apt and without extra downloads.
    return _install_wheel_binary("imageio-ffmpeg", "ffmpeg",
        "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())",
        "FFmpeg")


def _bin_dir() -> str:
    d = os.path.join(getattr(config, "DEPS_DIR", os.path.join(config.BASE_DIR, "deps")), "_bin")
    os.makedirs(d, exist_ok=True)
    return d


def _install_wheel_binary(pip_pkg: str, link_name: str, locate_code: str, label: str,
                          pip_spec: str | None = None) -> tuple[bool, str]:
    """pip-install a wheel that ships a binary, symlink it as `link_name`
    into deps/_bin and put that folder on PATH (this process + children)."""
    base = os.path.dirname(_bin_dir())
    target = os.path.join(base, "_tools", pip_pkg)
    ok, out = _pip_install(["--target", target, pip_spec or pip_pkg], config.BASE_DIR, 600)
    if not ok:
        return False, f"Could not install {label} automatically:\n{out}"
    env = os.environ.copy()
    env["PYTHONPATH"] = target
    try:
        res = subprocess.run([sys.executable, "-c", locate_code], env=env,
                             capture_output=True, text=True, timeout=120,
                             stdin=subprocess.DEVNULL)
        exe = res.stdout.strip().splitlines()[-1] if res.returncode == 0 and res.stdout.strip() else ""
    except Exception as e:
        return False, f"{label} install check failed: {e}"
    if not exe or not os.path.exists(exe):
        return False, f"{label} install did not produce a usable binary."
    try:
        os.chmod(exe, 0o755)
    except OSError:
        pass
    link = os.path.join(_bin_dir(), link_name)
    try:
        if os.path.lexists(link):
            os.remove(link)
        os.symlink(exe, link)
    except OSError:
        shutil.copy2(exe, link)
    add_bin_to_path()
    return True, f"Installed {label}. Restarting bot."


def add_bin_to_path():
    b = _bin_dir()
    parts = os.environ.get("PATH", "").split(os.pathsep)
    if b not in parts:
        os.environ["PATH"] = b + os.pathsep + os.environ.get("PATH", "")


def ensure_node() -> tuple[bool, str]:
    """Node.js 22 for hosted Node.js bots (npm/package.json projects)."""
    add_bin_to_path()
    if shutil.which("node") and shutil.which("npm"):
        try:
            r = subprocess.run(["npm", "--version"], capture_output=True, timeout=60,
                               stdin=subprocess.DEVNULL)
            if r.returncode == 0:
                return True, "Node.js is already installed."
        except Exception:
            pass
    ok, msg = _install_wheel_binary(
        "nodejs-wheel-binaries", "node",
        "import nodejs_wheel, os; print(os.path.join(os.path.dirname(nodejs_wheel.__file__), 'bin', 'node'))",
        "Node.js",
        pip_spec="nodejs-wheel-binaries>=22,<23")   # Node 22 LTS
    if ok:
        # The wheel's bin/npm is a stub that can't resolve ../lib/cli.js when
        # called through a link. Call the real npm CLI with node instead.
        base = os.path.join(os.path.dirname(_bin_dir()), "_tools", "nodejs-wheel-binaries")
        node = os.path.join(_bin_dir(), "node")
        for tool, js in (("npm", "npm-cli.js"), ("npx", "npx-cli.js")):
            cli = None
            for root, _dirs, files in os.walk(base):
                if js in files and os.path.basename(root) == "bin":
                    cli = os.path.join(root, js)
                    break
            if not cli:
                continue
            dst = os.path.join(_bin_dir(), tool)
            try:
                if os.path.lexists(dst):
                    os.remove(dst)
                with open(dst, "w") as f:
                    f.write(f'#!/bin/sh\nexec "{node}" "{cli}" "$@"\n')
                os.chmod(dst, 0o755)
            except OSError:
                pass
    return ok, msg


def ensure_runtime_tools() -> list[str]:
    """Called at panel startup: make sure ffmpeg + node exist (no root needed)."""
    add_bin_to_path()
    notes = []
    for fn in (ensure_ffmpeg, ensure_node):
        try:
            ok, msg = fn()
        except Exception as e:  # never block startup
            ok, msg = False, str(e)
        notes.append(("✅ " if ok else "⚠️ ") + msg.splitlines()[0])
    return notes
