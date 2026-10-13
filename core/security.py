"""
core/security.py  —  Multi-Layer Malware Detection Engine
==========================================================
Layer 1  — ZIP safety    (bomb, traversal, blocked extensions)
Layer 2  — HIGH risk     (1 match = instant block)
Layer 3  — MEDIUM score  (3+ matches = block)

Categories covered:
  A. Server/credential stealers
  B. Token stealers & database bots  
  C. Obfuscated / dynamic code
  D. Reverse shell & backdoors
  E. Crypto miners
  F. Ransomware / file destroyers
  G. Mass data exfiltration
  H. Phishing / credential harvesters
  I. Spam / DDoS bots
  J. Environment variable mass theft
  K. Webhook exfiltration
"""

import os
import re
import ast
import json
import stat
import base64
import zipfile
import config

MAX_UPLOAD_BYTES   = config.MAX_UPLOAD_MB * 1024 * 1024
MEDIUM_THRESHOLD   = 3
BLOCKED_EXTENSIONS = {".exe", ".dll", ".bat", ".msi", ".sh.exe", ".cmd", ".vbs", ".ps1"}
SOURCE_EXTS        = {".py", ".js", ".ts", ".sh", ".php", ".rb", ".go", ".java"}
SKIP_DIRS          = {"node_modules", "__pycache__", ".git", "venv", ".venv", "dist", "build", ".next", "vendor"}


# ══════════════════════════════════════════════════════════════
#  HIGH RISK — 1 bhi mila = INSTANT BLOCK
# ══════════════════════════════════════════════════════════════

HIGH_RISK = [

    # ── A. Server / Credential Stealers ─────────────────────
    (r"open\s*\(['\"]\/etc\/shadow",              "A1 - System password file read"),
    (r"Path\s*\(['\"]\/etc\/shadow",              "A1 - System password file read"),
    (r"['\"]\/etc\/shadow['\"]",                  "A1 - Shadow file reference"),
    (r"collect_vps_info",                          "A2 - VPS credential harvester"),
    (r"open\s*\(.*id_rsa",                        "A3 - SSH private key read"),
    (r"\.ssh.*id_rsa.*read",                      "A3 - SSH key reader"),
    (r"PASSWORD.{0,15}CREDENTIAL.{0,15}SCAN",     "A4 - Credential scanner marker"),

    # ── B. Token Stealers & Database Bots ───────────────────
    (r"scan_file_for_tokens",                      "B1 - Token stealer function"),
    (r"_TG_TOKEN_RE\s*=\s*re\.compile",           "B2 - Telegram token regex scanner"),
    (r"resolve_bot_username",                      "B3 - Stolen token validator"),
    (r"gather_all_files",                          "B4 - Mass file collector"),
    (r"CREATE TABLE.{0,30}bot_tokens",             "B5 - Token storage database"),
    (r"INSERT.{0,20}bot_tokens",                   "B5 - Token DB insert"),
    (r"def save_token\s*\(",                       "B6 - Token saver function"),
    (r"def get_token\s*\(",                        "B7 - Token getter function"),
    (r"def find_token\s*\(",                       "B7 - Token finder function"),
    (r"def list_tokens\s*\(",                      "B8 - Token lister function"),
    (r"CommandHandler.*addtoken",                  "B9 - Addtoken command"),
    (r"CommandHandler.*listtokens",                "B9 - Listtokens command"),
    (r"tokens\.db",                                "B10 - Token SQLite database"),
    (r"beta.hacker",                               "B11 - Known malware file"),

    # ── C. Obfuscated / Dynamic Code ────────────────────────
    (r"\beval\s*\(\s*base64",                      "C1 - Base64 eval obfuscation"),
    (r"\bexec\s*\(\s*base64",                      "C1 - Base64 exec obfuscation"),
    (r"\beval\s*\(\s*__import__\s*\(",             "C2 - Dynamic import eval"),
    (r"__import__\s*\(['\"]os['\"].*system",       "C3 - Hidden os.system call"),
    (r"compile\s*\(.*exec",                        "C4 - Compile+exec pattern"),
    (r"marshal\.loads",                            "C5 - Marshal deserialization"),
    (r"pickle\.loads\s*\(\s*base64",               "C5 - Pickle+base64 exploit"),
    (r"chr\s*\(\d+\)\s*\+\s*chr\s*\(\d+\)",       "C6 - Char-by-char obfuscation"),

    # ── D. Reverse Shell & Backdoors ────────────────────────
    (r"socket\.connect\s*\(.*\d+\.\d+\.\d+\.\d+", "D1 - Raw socket connection"),
    (r"subprocess.*\/bin\/sh.*socket",             "D2 - Reverse shell pattern"),
    (r"subprocess.*\/bin\/bash.*socket",           "D2 - Reverse shell pattern"),
    (r"os\.dup2\s*\(.*sock",                       "D3 - Socket dup2 (reverse shell)"),
    (r"pty\.spawn\s*\(['\"]\/bin",                 "D4 - PTY shell spawn"),
    (r"nc\s+-e\s+\/bin",                           "D5 - Netcat reverse shell"),
    (r"bash\s+-i\s+>&\s*\/dev\/tcp",               "D6 - Bash TCP reverse shell"),
    (r"\/dev\/tcp\/\d+\.\d+\.\d+\.\d+",           "D6 - Bash TCP redirect"),
    (r"paramiko.*exec_command",                    "D7 - SSH remote command exec"),
    (r"asyncssh.*run",                             "D7 - AsyncSSH remote exec"),
    (r"crontab.*curl.*sh|crontab.*wget.*sh",       "D8 - Cron-based persistence"),
    (r"at\s+now.*curl.*sh|at\s+now.*wget.*sh",    "D8 - AT-job persistence"),

    # ── E. Crypto Miners ────────────────────────────────────
    (r"xmrig|xmr-stak|cpuminer|cgminer",          "E1 - Crypto miner binary"),
    (r"stratum\+tcp://",                           "E2 - Mining pool protocol"),
    (r"monero|xmrpool|minexmr|supportxmr",        "E3 - Monero mining pool"),
    (r"hashrate|getwork|mining.subscribe",         "E4 - Mining protocol keywords"),
    (r"subprocess.*xmrig|os\.system.*xmrig",      "E5 - XMRig execution"),
    (r"wget.*xmrig|curl.*xmrig",                  "E5 - XMRig download"),

    # ── F. Ransomware / File Destroyers ─────────────────────
    (r"from\s+Crypto.*import.*AES.*encrypt",       "F1 - Mass AES encryption (ransomware)"),
    (r"for.*rglob.*encrypt|encrypt.*for.*rglob",  "F2 - Mass file encryptor"),
    (r"os\.remove.*for.*os\.walk",                 "F3 - Mass file deleter"),
    (r"shutil\.rmtree\s*\(\s*['\"]\/",            "F4 - Root directory destroyer"),
    (r"os\.system\s*\(['\"]rm\s+-rf\s+\/",        "F5 - rm -rf / command"),
    (r"subprocess.*rm.*-rf.*/home",               "F5 - Mass home dir delete"),

    # ── G. Mass Data Exfiltration ────────────────────────────
    (r"for.*os\.environ.*requests\.post",          "G1 - All env vars exfiltration"),
    (r"os\.environ\.items.*post|post.*os\.environ\.items", "G1 - Env dump exfiltration"),
    (r"open.*id_rsa.*post|post.*open.*id_rsa",    "G2 - SSH key exfiltration"),
    (r"subprocess.*cat.*\/etc.*post",             "G3 - System file exfiltration"),
    (r"\.read\(\).*send_document.*channel",       "G4 - Mass file send to channel"),

    # ── H. Phishing / Credential Harvesters ─────────────────
    (r"reply_text.*password\s*:\s*|reply_text.*enter.*password", "H1 - Password phishing prompt"),
    (r"reply_text.*credit.card|reply_text.*card.*number",        "H2 - Credit card phishing"),
    (r"reply_text.*send.*otp|reply_text.*enter.*otp.*steal|reply_text.*your.*otp.*is", "H3 - OTP phishing"),
    (r"context\.user_data\[.*password.*\]\s*=\s*update\.message\.text", "H4 - Password storage from message"),
    (r"save.*update\.message\.text.*password|password.*update\.message\.text", "H4 - Password harvester"),
    (r"reply_text.*seed.phrase|reply_text.*private.key.*wallet", "H5 - Crypto wallet phishing"),

    # ── I. DDoS Bots ────────────────────────────────────────
    (r"def\s+flood\s*\(|def\s+ddos\s*\(|def\s+attack\s*\(", "I1 - DDoS function definition"),
    (r"socket\.sendto.*while|while.*socket\.sendto", "I2 - UDP flood loop"),
    (r"threading.*target.*flood|multiprocessing.*flood", "I3 - Multi-threaded flooder"),
    (r"send_bytes.*loop.*target|flood.*send_bytes", "I4 - Byte flood pattern"),

    # ── J. Environment Variable Mass Theft ───────────────────
    (r"dict\s*\(\s*os\.environ\s*\)",             "J1 - Full env dump"),
    (r"os\.environ\.copy\s*\(\s*\)",              "J1 - Full env copy"),
    (r"json\.dumps\s*\(\s*dict\s*\(\s*os\.environ", "J2 - Env to JSON exfiltration"),
    (r"for\s+k.*v.*in\s+os\.environ\.items",      "J3 - Env items iteration for theft"),

    # ── K. Webhook Exfiltration ──────────────────────────────
    (r"requests\.post\s*\(.*discord\.com\/api\/webhooks", "K1 - Discord webhook exfil"),
    (r"requests\.post\s*\(.*hooks\.slack\.com",    "K2 - Slack webhook exfil"),
    (r"urllib.*discord.*webhook.*os\.environ",     "K3 - Discord webhook env steal"),
    (r"requests\.post\s*\(.*webhook.*password|requests\.post\s*\(.*webhook.*token",
                                                   "K4 - Webhook credential send"),

    # ── L. Advanced Obfuscation ─────────────────────────────
    (r"codecs\.decode.*rot.?13",                  "L1 - ROT13 obfuscation"),
    (r"bytes\.fromhex\s*\(",                    "L3 - Hex bytes decode"),
    (r"zlib\.decompress",                         "L4 - zlib decompress payload"),
    (r"__builtins__.*__import__",                  "L5 - Builtins injection"),
    (r"globals\s*\(\s*\)\[",                  "L6 - Dynamic globals access"),
    # ── M. Telegram exfil ────────────────────────────────────
    (r"send_document.*rglob|rglob.*send_document", "M1 - Recursive file sender"),
    (r"getUpdates.*for.*chat_id",                  "M2 - Token chat harvester"),
    # ── N. Cloud metadata ────────────────────────────────────
    (r"169[.]254[.]169[.]254",                     "N1 - AWS/GCP metadata endpoint"),
    (r"metadata[.]google[.]internal",              "N2 - GCP metadata endpoint"),
    # ── O. Persistence ───────────────────────────────────────
    (r"systemctl.*enable",                         "O1 - Systemd service install"),
    (r">>/etc/profile|>>/etc/bashrc",              "O2 - Shell profile hijack"),

]


# ══════════════════════════════════════════════════════════════
#  MEDIUM RISK — akele normal bhi hote hain, 3+ = block
# ══════════════════════════════════════════════════════════════

MEDIUM_RISK = [
    (r"api\.ipify\.org",                          "External IP fetch"),
    (r"icanhazip\.com",                           "External IP fetch"),
    (r"\/etc\/passwd",                            "Passwd file access"),
    (r"\.ssh[\\/]authorized_keys",               "SSH keys access"),
    (r"\/proc\/uptime",                           "System uptime access"),
    (r"broadcast_count|BC_COUNT\s*=\s*\d",       "Broadcast spam"),
    (r"socket\.gethostname",                     "Hostname fetch"),
    (r"platform\.release\s*\(\s*\)",             "OS info fetch"),
    (r"subprocess\.run.*find.*maxdepth",         "Filesystem discovery"),
    (r"subprocess\.run.*ps.*aux",                "Process lister"),
    (r"subprocess\.run.*ss.*-tln",               "Port scanner"),
    (r"REPL_ID.*REPL_SLUG|RAILWAY.*RENDER",     "Multi-platform targeting"),
    (r"send_document.*for.*in.*files",           "Mass file uploader"),
    (r"collect_platform|detect_platform",        "Platform detector"),
    (r"requests\.get\s*\(.*\{.*token",          "Token in external request"),
    (r"urllib\.request\.urlopen.*os\.environ",   "Env var in external request"),
    (r"smtplib.*SMTP",                           "Email sender (possible exfil)"),
    (r"base64\.b64encode\s*\(.*open\s*\(",      "File to base64 encoding"),
    (r"threading\.Thread.*target.*requests",     "Background HTTP requester"),
    (r"httpx[.]get|httpx[.]post|httpx[.]AsyncClient",  "HTTPX external request"),
    (r"pastebin[.]com|hastebin[.]com",                  "Paste-site exfil"),
    (r"ngrok|serveo[.]net|localhost[.]run",              "Tunnel service"),
    (r"for\\s+\\w+\\s+in\\s+Path.*rglob",       "Recursive path scan"),
    (r"send_message.*chat_id.*for.*in",                  "Mass message sender"),
    (r"reply_text.*verification.*code",              "Verification code message (check manually)"),

]


# ══════════════════════════════════════════════════════════════
#  COMMENT / DOCSTRING — whitelist (ignore these lines)
# ══════════════════════════════════════════════════════════════

WHITELIST = [r"^\s*#", r"^\s*\"\"\"", r"^\s*\'\'\'"]


# ══════════════════════════════════════════════════════════════
#  OBFUSCATION DETECTOR — base64 decoded content check
# ══════════════════════════════════════════════════════════════

def _check_base64_payloads(content: str) -> tuple[bool, str]:
    """Base64 strings ko decode karke unke andar bhi scan karo."""
    b64_pattern = re.compile(r'["\']([A-Za-z0-9+/]{40,}={0,2})["\']')
    for match in b64_pattern.finditer(content):
        try:
            decoded = base64.b64decode(match.group(1)).decode("utf-8", errors="ignore")
            for pattern, description in HIGH_RISK[:20]:  # Top patterns check
                if re.search(pattern, decoded, re.IGNORECASE):
                    return False, f"C0 - Obfuscated payload detected: {description}"
        except Exception:
            continue
    # Hex encoded payloads
    _hex_re = re.compile(r'[^"]{0}["\']+([0-9a-fA-F]{40,})["\']+')
    for _m in _hex_re.finditer(content):
        try:
            _decoded = bytes.fromhex(_m.group(1)).decode("utf-8", errors="ignore")
            for _pat, _desc in HIGH_RISK:
                if re.search(_pat, _decoded, re.IGNORECASE):
                    return False, f"L3 - Hex payload: {_desc}"
        except Exception:
            continue
    return True, ""


# ══════════════════════════════════════════════════════════════
#  PYTHON AST CHECK — dangerous function calls
# ══════════════════════════════════════════════════════════════

DANGEROUS_CALLS = {
    "eval", "exec", "compile", "__import__",
    "os.system", "subprocess.call", "subprocess.Popen",
}

def _ast_check(content: str, filename: str) -> tuple[bool, str]:
    """Python AST se dangerous patterns pakdo jo regex miss kar sake."""
    if not filename.endswith(".py"):
        return True, ""
    try:
        tree = ast.parse(content)
        for node in ast.walk(tree):
            # eval/exec seedha call
            if isinstance(node, ast.Call):
                func = node.func
                name = ""
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = f"{getattr(func.value, 'id', '')} .{func.attr}"
                if name in {"eval", "exec"} and node.args:
                    arg = node.args[0]
                    # Agar argument base64 call hai
                    if isinstance(arg, ast.Call):
                        inner = getattr(getattr(arg, "func", None), "attr", "")
                        if inner in {"b64decode", "decodebytes", "decodestring"}:
                            return False, "C1 - eval(base64.decode(...)) detected in AST"
    except SyntaxError:
        pass  # Invalid Python — regex already handles it
    except Exception:
        pass
    return True, ""


# ══════════════════════════════════════════════════════════════
#  CORE SCAN FUNCTIONS
# ══════════════════════════════════════════════════════════════

def _is_comment(line: str) -> bool:
    for pat in WHITELIST:
        if re.match(pat, line):
            return True
    return False


def scan_file_for_malware(filepath: str) -> tuple[bool, str, int]:
    """
    Returns: (is_safe, reason_if_blocked, medium_score)
    """
    try:
        with open(filepath, "r", errors="ignore") as f:
            content = f.read()
    except (OSError, PermissionError) as exc:
        return False, f"Cannot inspect source file safely ({type(exc).__name__}).", 0

    lines    = content.splitlines()
    filename = os.path.basename(filepath)

    # ── HIGH RISK check ──────────────────────────────────────
    for pattern, description in HIGH_RISK:
        for i, line in enumerate(lines, 1):
            if _is_comment(line):
                continue
            if re.search(pattern, line, re.IGNORECASE | re.DOTALL):
                short = line.strip()[:80]
                return False, (
                    f"🚨 *Malicious code blocked!*\n"
                    f"Category: `{description}`\n"
                    f"File: `{filename}` line {i}\n"
                    f"Code: `{short}`"
                ), 0

    # ── OBFUSCATION check ────────────────────────────────────
    ok, reason = _check_base64_payloads(content)
    if not ok:
        return False, f"🚨 *Obfuscated malware detected!*\n{reason}", 0

    # ── AST check (Python only) ──────────────────────────────
    ok, reason = _ast_check(content, filename)
    if not ok:
        return False, f"🚨 *AST analysis: dangerous code!*\n{reason}", 0

    # ── MEDIUM RISK score ────────────────────────────────────
    medium_score = 0
    for pattern, _ in MEDIUM_RISK:
        for line in lines:
            if _is_comment(line):
                continue
            if re.search(pattern, line, re.IGNORECASE):
                medium_score += 1
                break

    return True, "", medium_score


# ══════════════════════════════════════════════════════════════
#  v5.2 — LINK / MANIFEST HARDENING
# ══════════════════════════════════════════════════════════════

NPM_LIFECYCLE = {"preinstall", "install", "postinstall", "prepare", "prepublish",
                 "preprepare", "postprepare"}
PIP_BAD_OPTIONS = ("--index-url", "-i ", "--extra-index-url", "--find-links", "-f ",
                   "--trusted-host", "--no-index", "-e ", "--editable", "--constraint", "-c ")


def sanitize_project_tree(project_path: str) -> tuple[bool, str]:
    """Deletes every symlink / device / fifo inside the project and refuses
    hard-linked files. A symlink like readme.txt -> /app/config.py is the
    classic way to make backup/file-browser/builder read the PANEL's secrets.
    Must run on EVERY source (ZIP, GitHub, single file) before anything else."""
    if not os.path.isdir(project_path):
        return True, ""
    for cur, dirs, files in os.walk(project_path, topdown=True, followlinks=False):
        for name in list(dirs) + list(files):
            fp = os.path.join(cur, name)
            try:
                st = os.lstat(fp)
            except OSError:
                continue
            mode = st.st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                try:
                    os.unlink(fp)
                except OSError:
                    return False, f"Could not remove unsafe link/special file: `{name}`"
            elif stat.S_ISREG(mode) and st.st_nlink > 1:
                return False, f"Hard-linked file not allowed: `{name}`"
        dirs[:] = [d for d in dirs if os.path.isdir(os.path.join(cur, d))
                   and not os.path.islink(os.path.join(cur, d))]
    return True, ""


def check_manifests(project_path: str) -> tuple[bool, str]:
    """package.json / requirements.txt are NOT source files, so the regex
    scanner never looked at them. Block the install-time code-exec tricks."""
    pj = os.path.join(project_path, "package.json")
    if os.path.isfile(pj):
        try:
            with open(pj, "r", encoding="utf-8", errors="ignore") as f:
                data = json.load(f)
        except Exception:
            return False, "package.json is not valid JSON."
        if not isinstance(data, dict):
            return False, "package.json must be a JSON object."
        scripts = data.get("scripts") or {}
        if isinstance(scripts, dict):
            bad = sorted(k for k in scripts if k in NPM_LIFECYCLE)
            if bad:
                return False, ("package.json has install-time scripts (" + ", ".join(bad) +
                               ") — not allowed. Use only a `start` script.")
        for section in ("dependencies", "optionalDependencies", "devDependencies"):
            deps = data.get(section) or {}
            if isinstance(deps, dict):
                for name, spec in deps.items():
                    if isinstance(spec, str) and spec.strip().lower().startswith(("file:", "link:")):
                        return False, f"Local path dependency not allowed: `{name}`"
    req = os.path.join(project_path, "requirements.txt")
    if os.path.isfile(req):
        try:
            with open(req, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.read().splitlines()
        except OSError:
            return False, "Cannot read requirements.txt."
        for ln in lines:
            t = ln.strip().lower()
            if not t or t.startswith("#"):
                continue
            if t.startswith("-") and any(t.startswith(opt) for opt in PIP_BAD_OPTIONS):
                return False, "requirements.txt uses a blocked pip option (custom index / editable / constraints)."
            if "file:" in t or t.startswith(("/", "./", "../")):
                return False, "requirements.txt references a local path — not allowed."
    return True, ""


def scan_project_tree(project_path: str) -> tuple[bool, str]:
    """Poore project ko scan karo — sab files."""
    ok, reason = sanitize_project_tree(project_path)
    if not ok:
        return False, reason
    ok, reason = check_manifests(project_path)
    if not ok:
        return False, reason
    total_size         = 0
    total_medium_score = 0
    medium_files       = []

    for root, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]

        for name in files:
            fp  = os.path.join(root, name)
            ext = os.path.splitext(name)[1].lower()

            ok, reason = check_extension(name)
            if not ok:
                return False, reason

            try:
                total_size += os.path.getsize(fp)
            except OSError:
                continue

            if ext in SOURCE_EXTS:
                ok, reason, score = scan_file_for_malware(fp)
                if not ok:
                    return False, reason
                if score:
                    total_medium_score += score
                    medium_files.append(f"`{name}`: {score} flags")

    if total_medium_score >= MEDIUM_THRESHOLD:
        details = "\n".join(medium_files[:5])
        return False, (
            f"🚨 *Multiple suspicious patterns!*\n"
            f"Score: {total_medium_score}/{MEDIUM_THRESHOLD}\n"
            f"Files:\n{details}"
        )

    if total_size > MAX_UPLOAD_BYTES * 3:
        return False, "Project too large after extraction."

    return True, ""


# ══════════════════════════════════════════════════════════════
#  ZIP SAFETY
# ══════════════════════════════════════════════════════════════

def scan_zip_before_extract(zip_path: str) -> tuple[bool, str]:
    try:
        with zipfile.ZipFile(zip_path) as zf:
            total = sum(i.file_size for i in zf.infolist())
            if total > MAX_UPLOAD_BYTES * 5:
                return False, f"ZIP bomb suspected ({total // 1024 // 1024}MB uncompressed)."
            for member in zf.namelist():
                if ".." in member or member.startswith("/"):
                    return False, f"Path traversal in ZIP: `{member}`"
                ok, reason = check_extension(member)
                if not ok:
                    return False, reason
    except zipfile.BadZipFile:
        return False, "Corrupted or invalid ZIP file."
    return True, ""


# ══════════════════════════════════════════════════════════════
#  BASIC CHECKS
# ══════════════════════════════════════════════════════════════

def check_upload_size(file_path: str) -> tuple[bool, str]:
    size = os.path.getsize(file_path)
    if size > MAX_UPLOAD_BYTES:
        return False, f"File too large ({size // 1024 // 1024}MB, limit {config.MAX_UPLOAD_MB}MB)."
    return True, ""


def check_extension(filename: str) -> tuple[bool, str]:
    ext = os.path.splitext(filename)[1].lower()
    if ext in BLOCKED_EXTENSIONS:
        return False, f"File type `{ext}` is not allowed."
    return True, ""


def enforce_user_bot_limit(current_count: int, is_admin: bool, is_premium: bool = False) -> tuple[bool, str]:
    if is_admin:
        return True, ""
    limit = config.MAX_BOTS_PER_PREMIUM_USER if is_premium else config.MAX_BOTS_PER_USER
    if current_count >= limit:
        tier = "premium" if is_premium else "free"
        return False, f"You've reached your {tier} bot limit ({limit})."
    return True, ""
