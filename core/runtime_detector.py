"""
core/runtime_detector.py
Inspects an uploaded/cloned project folder and figures out:
  - which runtime it needs (python / node)
  - what the entrypoint file is

v2 fix: entrypoint detection used to ONLY match a fixed filename list
(index.js/main.js/app.js/bot.js/server.js) or an exact package.json
"main" field. Any project with a custom-named entry file (e.g.
telegramBot.js, whatsapp.js, wa-bot.js — extremely common for
Telegraf/Baileys bots) silently fell through to entrypoint=None, which
deploy.py then rejected with "Couldn't detect a runnable entrypoint" —
i.e. the Node bot would never host, no matter how correct the code was.
Detection is now, in order:
  1. package.json "main" field
  2. package.json "scripts.start" (e.g. "start": "node telegramBot.js")
  3. the fixed common filenames (index.js, etc.)
  4. if exactly one .js/.mjs/.cjs file sits at the project root, use it
  5. otherwise raise a clear NodeEntrypointAmbiguous error listing the
     candidates found, so the user can fix package.json instead of
     getting a vague failure.
"""

import os
import re
import json

PYTHON_ENTRY_CANDIDATES = ["main.py", "bot.py", "app.py", "run.py", "__main__.py"]
NODE_ENTRY_CANDIDATES = ["index.js", "main.js", "app.js", "bot.js", "server.js"]
NODE_JS_EXTENSIONS = (".js", ".mjs", ".cjs")

# Matches the "node <file>" (or "nodejs <file>") part of a package.json
# "scripts.start" value, e.g. "node telegramBot.js" or "node ./bot.js".
NODE_START_SCRIPT_RE = re.compile(r"\bnodejs?\s+(?:\.\/)?([\w\-./]+\.(?:js|mjs|cjs))\b")


class NodeEntrypointAmbiguous(Exception):
    """Raised when a Node project has multiple plausible entry files and
    none of package.json's main/scripts.start/known-filename hints match."""


def detect_runtime(project_path: str):
    """
    Returns a dict: {"runtime": "python"|"node"|None, "entrypoint": str|None,
                      "dependency_file": str|None}
    Raises NodeEntrypointAmbiguous if it's clearly a Node project but the
    entry file can't be determined unambiguously (caller should surface
    this message to the user rather than a generic failure).
    """
    files = set(os.listdir(project_path))

    # Node.js project
    if "package.json" in files:
        entry = (
            _find_package_json_main(project_path)
            or _find_package_json_start_script(project_path)
            or _first_match(files, NODE_ENTRY_CANDIDATES)
            or _single_unambiguous_js_file(project_path)
        )
        return {
            "runtime": "node",
            "entrypoint": entry,
            "dependency_file": "package.json",
        }

    # A .js/.mjs/.cjs project with no package.json at all (single-file bot)
    if any(f.endswith(NODE_JS_EXTENSIONS) for f in files) and not any(f.endswith(".py") for f in files):
        entry = _first_match(files, NODE_ENTRY_CANDIDATES) or _single_unambiguous_js_file(project_path)
        return {
            "runtime": "node",
            "entrypoint": entry,
            "dependency_file": None,
        }

    # Python project
    if "requirements.txt" in files or any(f.endswith(".py") for f in files):
        entry = _first_match(files, PYTHON_ENTRY_CANDIDATES)
        if not entry:
            py_files = [f for f in files if f.endswith(".py")]
            entry = py_files[0] if py_files else None
        return {
            "runtime": "python",
            "entrypoint": entry,
            "dependency_file": "requirements.txt" if "requirements.txt" in files else None,
        }

    return {"runtime": None, "entrypoint": None, "dependency_file": None}


def _first_match(files, candidates):
    for c in candidates:
        if c in files:
            return c
    return None


def _load_package_json(project_path: str):
    pkg_path = os.path.join(project_path, "package.json")
    try:
        with open(pkg_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _find_package_json_main(project_path: str):
    data = _load_package_json(project_path)
    main = data.get("main")
    if main and os.path.exists(os.path.join(project_path, main)):
        return main
    return None


def _find_package_json_start_script(project_path: str):
    data = _load_package_json(project_path)
    start = (data.get("scripts") or {}).get("start")
    if not start:
        return None
    m = NODE_START_SCRIPT_RE.search(start)
    if not m:
        return None
    candidate = m.group(1)
    if os.path.exists(os.path.join(project_path, candidate)):
        return candidate
    return None


def _single_unambiguous_js_file(project_path: str):
    """
    Last-resort fallback: if exactly one JS file sits at the project
    root (ignoring node_modules/hidden/config files), that's almost
    certainly the entrypoint. If there's more than one, we deliberately
    don't guess — raise so the user gets an actionable error instead of
    the wrong file being launched.
    """
    root_js_files = [
        f for f in os.listdir(project_path)
        if f.endswith(NODE_JS_EXTENSIONS)
        and not f.startswith(".")
        and os.path.isfile(os.path.join(project_path, f))
    ]
    if len(root_js_files) == 1:
        return root_js_files[0]
    if len(root_js_files) > 1:
        raise NodeEntrypointAmbiguous(
            "Found multiple JS files and no package.json \"main\"/\"scripts.start\" "
            f"pointing at one of them: {', '.join(sorted(root_js_files))}. "
            'Add e.g. "main": "yourfile.js" to package.json (or a "start" script '
            'like "node yourfile.js") and re-upload.'
        )
    return None
