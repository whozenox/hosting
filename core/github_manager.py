"""
core/github_manager.py
Clones a public (or token-authenticated) GitHub repo into the uploads
directory so it can go through the same deploy pipeline as an upload.
"""

import os
import re
import shutil
import config
from core import security

try:
    import git
    from git.exc import GitCommandError
except ImportError:
    git = None
    GitCommandError = Exception

GITHUB_URL_RE = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+(\.git)?/?$")


def is_valid_github_url(url: str) -> bool:
    return bool(GITHUB_URL_RE.match(url.strip()))


def clone_repo(repo_url: str, user_id: int, bot_name: str) -> tuple[bool, str]:
    """
    Clones repo_url into uploads/<user_id>/<bot_name>.
    Returns (success, path_or_error_message).
    """
    if git is None:
        return False, "GitPython isn't installed on the server."

    if not is_valid_github_url(repo_url):
        return False, "That doesn't look like a valid GitHub repo URL."

    dest = os.path.join(config.UPLOADS_DIR, str(user_id), bot_name)
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.makedirs(dest, exist_ok=True)

    try:
        # GIT_TERMINAL_PROMPT=0: a private / mistyped repo must fail right
        # away instead of waiting forever for a username on a console
        # nobody can type into (that froze a worker thread).
        # SECURITY v5.2: no symlinks (core.symlinks=false -> checked out as plain
        # text files), no hooks, https only, no submodule/ext:: tricks.
        git.Repo.clone_from(
            repo_url, dest, depth=1, single_branch=True, no_tags=True,
            env={
                "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo",
                "GIT_ALLOW_PROTOCOL": "https",
                "GIT_CONFIG_COUNT": "3",
                "GIT_CONFIG_KEY_0": "core.symlinks", "GIT_CONFIG_VALUE_0": "false",
                "GIT_CONFIG_KEY_1": "core.hooksPath", "GIT_CONFIG_VALUE_1": "/dev/null",
                "GIT_CONFIG_KEY_2": "protocol.file.allow", "GIT_CONFIG_VALUE_2": "never",
            },
        )
    except GitCommandError as e:
        shutil.rmtree(dest, ignore_errors=True)
        return False, "Clone failed — is the repo public and does the URL exist?"
    except Exception as e:
        shutil.rmtree(dest, ignore_errors=True)
        return False, f"Clone failed: {e}"

    # Don't ship the .git history into the deploy/container image
    git_dir = os.path.join(dest, ".git")
    if os.path.exists(git_dir):
        shutil.rmtree(git_dir, ignore_errors=True)

    # Belt and braces: strip any link / special file and refuse oversized repos.
    ok, reason = security.sanitize_project_tree(dest)
    if not ok:
        shutil.rmtree(dest, ignore_errors=True)
        return False, reason
    for junk in (".gitmodules", ".gitattributes"):
        try:
            os.remove(os.path.join(dest, junk))
        except OSError:
            pass
    total = 0
    for root, _d, files in os.walk(dest):
        for n in files:
            try:
                total += os.path.getsize(os.path.join(root, n))
            except OSError:
                pass
    if total > config.MAX_UPLOAD_MB * 3 * 1024 * 1024:
        shutil.rmtree(dest, ignore_errors=True)
        return False, "Repository is too large."

    return True, dest


def pull_latest(project_path: str) -> tuple[bool, str]:
    """Not used after .git is stripped on clone; kept for repos re-cloned with history."""
    if git is None:
        return False, "GitPython isn't installed on the server."
    try:
        repo = git.Repo(project_path)
        repo.remotes.origin.pull()
        return True, "Updated to latest commit."
    except Exception as e:
        return False, str(e)
