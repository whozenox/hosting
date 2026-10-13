"""
core/process_guard.py — fork-bomb guard.
Landlock/rlimits per-bot process count nahi rokte (RLIMIT_NPROC poore UID par
lagta hai), isliye monitor har tick par bot ke process-tree ka size check
karta hai aur limit paar karne par poora tree kill kar deta hai.
"""
import psutil


def tree_size(pid: int) -> int:
    try:
        return 1 + len(psutil.Process(pid).children(recursive=True))
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return 0


def over_limit(pid: int, max_procs: int) -> int:
    """Return process count if > max_procs, else 0."""
    n = tree_size(pid)
    return n if n > max_procs else 0
