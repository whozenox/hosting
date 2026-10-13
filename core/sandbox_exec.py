"""
core/sandbox_exec.py  —  Landlock launcher (STANDALONE, no panel imports)

Docker ke bina (Pterodactyl / shared host) ek hosted bot ko kernel-level
filesystem sandbox me chalata hai.

    python -I sandbox_exec.py <b64-json-policy> -- <cmd> [args...]
    python -I sandbox_exec.py --probe

Kya karta hai:
  1. NO_NEW_PRIVS set karta hai
  2. Landlock ruleset banata hai: bot sirf policy me diye paths hi dekh/likh sakta hai
     (panel ka config.py, database/, doosre users ke folders, .env => EACCES)
  3. rlimits lagata hai
  4. exec() karta hai (same PID => psutil/monitor ko koi farq nahi)

FAIL-CLOSED: agar Landlock lag nahi paya to bot START HI NAHI hota (exit 97).
"""

import base64
import ctypes
import json
import os
import resource
import shutil
import sys

EXIT_SANDBOX_FAIL = 97

SYS_CREATE, SYS_ADD, SYS_RESTRICT = 444, 445, 446

EXEC, WRITE_FILE, READ_FILE, READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
REMOVE_DIR, REMOVE_FILE = 1 << 4, 1 << 5
MAKE_CHAR, MAKE_DIR, MAKE_REG, MAKE_SOCK = 1 << 6, 1 << 7, 1 << 8, 1 << 9
MAKE_FIFO, MAKE_BLOCK, MAKE_SYM = 1 << 10, 1 << 11, 1 << 12
REFER, TRUNCATE, IOCTL_DEV = 1 << 13, 1 << 14, 1 << 15

FILE_ONLY_BITS = EXEC | WRITE_FILE | READ_FILE | TRUNCATE | IOCTL_DEV

PR_SET_NO_NEW_PRIVS = 38

_libc = ctypes.CDLL(None, use_errno=True)


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def _err(msg: str):
    sys.stderr.write(f"[sandbox] {msg}\n")
    sys.stderr.flush()


def abi_version() -> int:
    r = _libc.syscall(SYS_CREATE, None, 0, 1)   # LANDLOCK_CREATE_RULESET_VERSION
    return r if r > 0 else 0


def _handled_mask(abi: int) -> int:
    m = (1 << 13) - 1                 # bits 0..12  (ABI 1)
    if abi >= 2:
        m |= REFER
    if abi >= 3:
        m |= TRUNCATE
    if abi >= 5:
        m |= IOCTL_DEV
    return m


def _masks(abi: int) -> dict:
    handled = _handled_mask(abi)
    ro = (READ_FILE | READ_DIR) & handled
    rx = ro | EXEC
    write = (WRITE_FILE | REMOVE_DIR | REMOVE_FILE | MAKE_CHAR | MAKE_DIR | MAKE_REG |
             MAKE_SOCK | MAKE_FIFO | MAKE_BLOCK | MAKE_SYM | REFER | TRUNCATE) & handled
    return {"handled": handled, "ro": ro, "rx": rx, "rw": ro | write, "rwx": rx | write,
            "dev": (READ_FILE | WRITE_FILE | TRUNCATE | IOCTL_DEV) & handled,
            "devro": READ_FILE & handled}


def _add_path(ruleset_fd: int, path: str, access: int):
    try:
        fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    except OSError:
        return                              # path nahi hai => skip
    try:
        if not os.path.isdir(path):
            access &= FILE_ONLY_BITS
        if not access:
            return
        attr = _PathBeneath(access, fd)
        if _libc.syscall(SYS_ADD, ruleset_fd, 1, ctypes.byref(attr), 0) != 0:
            e = ctypes.get_errno()
            raise OSError(e, f"landlock_add_rule({path}): {os.strerror(e)}")
    finally:
        os.close(fd)


def apply_landlock(policy: dict):
    abi = abi_version()
    if abi < 1:
        raise OSError("Landlock kernel me available nahi hai (ya seccomp ne block kiya)")
    mk = _masks(abi)
    attr = _RulesetAttr(mk["handled"])
    rfd = _libc.syscall(SYS_CREATE, ctypes.byref(attr), ctypes.sizeof(attr), 0)
    if rfd < 0:
        e = ctypes.get_errno()
        raise OSError(e, f"landlock_create_ruleset: {os.strerror(e)}")
    try:
        for kind in ("ro", "rx", "rw", "rwx"):
            for p in policy.get(kind, []):
                _add_path(rfd, p, mk[kind])
        for p in policy.get("dev", []):
            _add_path(rfd, p, mk["dev"])
        for p in policy.get("devro", []):
            _add_path(rfd, p, mk["devro"])
        if _libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl(NO_NEW_PRIVS) failed")
        if _libc.syscall(SYS_RESTRICT, rfd, 0) != 0:
            e = ctypes.get_errno()
            raise OSError(e, f"landlock_restrict_self: {os.strerror(e)}")
    finally:
        os.close(rfd)


def apply_limits(limits: dict):
    def _set(res, soft, hard=None):
        try:
            cur_soft, cur_hard = resource.getrlimit(res)
            hard = soft if hard is None else hard
            if cur_hard != resource.RLIM_INFINITY:
                soft = min(soft, cur_hard)
                hard = min(hard, cur_hard)
            resource.setrlimit(res, (soft, hard))
        except (ValueError, OSError):
            pass

    _set(resource.RLIMIT_CORE, 0)
    if limits.get("nofile"):
        _set(resource.RLIMIT_NOFILE, int(limits["nofile"]))
    if limits.get("fsize_mb"):
        _set(resource.RLIMIT_FSIZE, int(limits["fsize_mb"]) * 1024 * 1024)
    if limits.get("as_mb"):
        _set(resource.RLIMIT_AS, int(limits["as_mb"]) * 1024 * 1024)
    if limits.get("cpu_seconds"):
        _set(resource.RLIMIT_CPU, int(limits["cpu_seconds"]))


def main(argv: list) -> int:
    if len(argv) >= 2 and argv[1] == "--probe":
        try:
            apply_landlock({"rx": ["/usr", "/lib", "/lib64", "/bin"], "ro": ["/etc"]})
            print(f"ok abi={abi_version()}")
            return 0
        except Exception as e:
            print(f"fail {e}")
            return EXIT_SANDBOX_FAIL

    if len(argv) < 4 or argv[2] != "--":
        _err("usage: sandbox_exec.py <policy> -- cmd...")
        return 2
    try:
        policy = json.loads(base64.b64decode(argv[1]).decode())
    except Exception as e:
        _err(f"bad policy: {e}")
        return 2
    cmd = argv[3:]

    # exec path pehle resolve karo (restrict ke baad PATH-search kaam nahi karega)
    exe = shutil.which(cmd[0]) or cmd[0]

    try:
        if policy.get("cwd"):
            os.chdir(policy["cwd"])
        apply_limits(policy.get("limits", {}))
        # "@self" => is process ka apna PID (exec ke baad bhi same rehta hai)
        me = f"/proc/{os.getpid()}"
        for k in ("ro", "rx", "rw", "rwx"):
            policy[k] = [p.replace("/proc/@self", me) for p in policy.get(k, [])]
        apply_landlock(policy)
    except Exception as e:
        _err(f"SECURITY BLOCK: sandbox apply nahi hua, bot start nahi kiya: {e}")
        return EXIT_SANDBOX_FAIL

    try:
        os.execve(exe, cmd, os.environ)
    except OSError as e:
        _err(f"exec failed: {e}")
        return 127
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
