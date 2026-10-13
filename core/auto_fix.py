"""
core/auto_fix.py

Safe, log-aware automatic repair for hosted bots.

Important fixes in v4.2:
- Uses the *latest* ModuleNotFoundError in the log instead of the first one.
  This prevents an old `telebot` error from causing a newer `flask` crash to
  install pyTelegramBotAPI.
- Keeps a proper import-name -> PyPI-name mapping (e.g. flask -> Flask,
  telegram -> python-telegram-bot).
- Verifies that the missing import can actually be imported after pip install.
- Refuses to claim success when pip installed a package but the original
  import is still unavailable.
- Supports a larger set of common Python/Node packages.
"""

import re
from core import dependency_manager

# Match the actual exception emitted by Python. We intentionally collect ALL
# matches and use the last one because a log file often contains several
# historical crash loops.
PY_MODULE_NOT_FOUND = re.compile(
    r"ModuleNotFoundError:\s*No module named ['\"]([^'\"]+)['\"]"
)
NODE_MODULE_NOT_FOUND = re.compile(
    r"Cannot find module ['\"]([^'\"]+)['\"]"
)

# Python import name -> PyPI distribution name.
# Keep import names as they appear in `import ...` / traceback messages.
IMPORT_TO_PIP = {
    # Web / API
    "flask": "Flask",
    "fastapi": "fastapi",
    "starlette": "starlette",
    "uvicorn": "uvicorn",
    "aiohttp": "aiohttp",
    "httpx": "httpx",
    "requests": "requests",
    "urllib3": "urllib3",
    "websockets": "websockets",
    "websocket": "websocket-client",
    "bs4": "beautifulsoup4",
    "lxml": "lxml",

    # Telegram / Discord / chat
    "telegram": "python-telegram-bot",
    "telebot": "pyTelegramBotAPI",
    "pyrogram": "Pyrogram",
    "pytgcalls": "py-tgcalls",
    "discord": "discord.py",
    "nextcord": "nextcord",
    "disnake": "disnake",

    # Media / YouTube
    "yt_dlp": "yt-dlp",
    "youtube_dl": "youtube_dl",
    "youtube_search": "youtube-search-python",
    "pytube": "pytube",
    "ytmusicapi": "ytmusicapi",
    "ffmpeg": "ffmpeg-python",
    "moviepy": "moviepy",
    "mutagen": "mutagen",

    # Data / image / ML
    "PIL": "Pillow",
    "cv2": "opencv-python-headless",
    "numpy": "numpy",
    "pandas": "pandas",
    "yaml": "PyYAML",
    "sklearn": "scikit-learn",
    "scipy": "scipy",
    "matplotlib": "matplotlib",

    # Auth / crypto / config
    "dotenv": "python-dotenv",
    "jwt": "PyJWT",
    "Crypto": "pycryptodome",
    "cryptography": "cryptography",
    "dateutil": "python-dateutil",
    "magic": "python-magic",

    # Database / filesystem / misc
    "sqlalchemy": "SQLAlchemy",
    "redis": "redis",
    "pymongo": "pymongo",
    "motor": "motor",
    "psutil": "psutil",
    "watchdog": "watchdog",
    "aiofiles": "aiofiles",
    "serial": "pyserial",
    "git": "GitPython",
    "paramiko": "paramiko",
    "qrcode": "qrcode",
    "openpyxl": "openpyxl",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "py7zr": "py7zr",
}


def _top_level(module: str) -> str:
    return module.split(".", 1)[0].strip()


def pip_name_for(module: str) -> str:
    """Return the PyPI distribution for an import/module name."""
    top = _top_level(module)
    return IMPORT_TO_PIP.get(top, top)


def _last_match(pattern: re.Pattern, log_text: str) -> str | None:
    matches = list(pattern.finditer(log_text or ""))
    if not matches:
        return None
    return matches[-1].group(1).strip()


def diagnose(log_text: str) -> dict:
    """Return the newest actionable error from the supplied log."""
    if not log_text:
        return {"issue": None, "detail": None}

    # IMPORTANT: take the newest occurrence, not the first occurrence.
    py_mod = _last_match(PY_MODULE_NOT_FOUND, log_text)
    node_mod = _last_match(NODE_MODULE_NOT_FOUND, log_text)

    # Compare positions when both Python and Node patterns exist in a mixed
    # log. The error nearest the end is the one that actually caused the
    # latest crash.
    py_pos = log_text.rfind("ModuleNotFoundError") if py_mod else -1
    node_pos = log_text.rfind("Cannot find module") if node_mod else -1

    if py_pos >= node_pos and py_mod:
        return {"issue": "missing_python_package", "detail": py_mod}
    if node_mod:
        return {"issue": "missing_node_package", "detail": node_mod}

    if re.search(r"FFmpegNotInstalled|ffmpeg.{0,40}(not installed|not found|No such file)|No such file or directory: 'ffmpeg'",
                 log_text, re.IGNORECASE):
        return {"issue": "missing_ffmpeg", "detail": None}

    if "SyntaxError" in log_text:
        return {"issue": "syntax_error", "detail": None}

    if "Permission denied" in log_text:
        return {"issue": "permission_denied", "detail": None}

    return {"issue": None, "detail": None}


_SAFE_PKG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}$")


def _add_to_requirements(project_path: str, package: str) -> bool:
    """Appends `package` to the project's requirements.txt (no install here!).
    The package gets installed INSIDE the Docker build, never on the panel
    host. Returns True if the file was changed."""
    import os
    if not _SAFE_PKG.match(package):
        return False
    req = os.path.join(project_path, "requirements.txt")
    existing = ""
    if os.path.isfile(req):
        with open(req, "r", encoding="utf-8", errors="ignore") as f:
            existing = f.read()
    names = {re.split(r"[<>=!~\[; ]", ln.strip(), 1)[0].lower().replace("_", "-")
             for ln in existing.splitlines() if ln.strip() and not ln.strip().startswith(("#", "-"))}
    if package.lower().replace("_", "-") in names:
        return False
    with open(req, "a", encoding="utf-8") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(package + "\n")
    return True


def attempt_fix(
    project_path: str,
    runtime: str,
    log_text: str,
    bot_id: int | None = None,
) -> tuple[bool, str]:
    """Attempt one safe automatic repair and return (fixed, message).

    SECURITY v5.2: bot logs are attacker-controlled text. Auto-fix therefore
    NEVER runs pip/npm/apt on the panel host and NEVER installs a package name
    taken straight from a log. It only (a) adds a package from the built-in
    allow-list to requirements.txt, and (b) asks for a Docker rebuild, where
    the install happens inside the sandboxed build.
    """
    diagnosis = diagnose(log_text)
    issue = diagnosis["issue"]

    if issue == "missing_python_package":
        module = diagnosis["detail"]
        top = _top_level(module)
        if top not in IMPORT_TO_PIP:
            return False, (
                f"Module '{top}' is missing. For safety, Auto-Fix only installs well-known "
                f"packages. Add it to your requirements.txt and re-upload."
            )
        package = IMPORT_TO_PIP[top]
        changed = _add_to_requirements(project_path, package)
        if not changed:
            return False, (
                f"'{package}' is already in requirements.txt but import '{top}' still fails. "
                f"Check the package name/version in your requirements.txt."
            )
        return True, f"Added '{package}' to requirements.txt for import '{top}'. Rebuilding bot."

    if issue == "missing_node_package":
        import os
        if not os.path.isfile(os.path.join(project_path, "package.json")):
            return False, "A Node module is missing but there is no package.json. Add one listing your dependencies."
        return True, "Node module missing — rebuilding the container so npm install runs inside Docker."

    if issue == "missing_ffmpeg":
        return False, (
            "ffmpeg is not available inside the bot container. Use a pip package that bundles it "
            "(e.g. add 'imageio-ffmpeg' to requirements.txt)."
        )

    if issue == "syntax_error":
        return False, (
            "A syntax error was detected in your code. Auto-Fix can't safely "
            "rewrite source code — please fix the syntax shown in the logs."
        )

    if issue == "permission_denied":
        return False, (
            "A permission error was detected. Check the file path and "
            "permissions in your code."
        )

    return False, "No known auto-fixable issue was found in the recent logs."
