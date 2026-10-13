"""
core/crypto_store.py — encryption at rest for sensitive panel data.

Key SIRF env var PANEL_ENC_KEY se aati hai (disk par kabhi nahi likhi jaati).
Generate:  python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"

Key set nahi hai => purana plaintext behaviour (warning log ke saath).
Purana plaintext data bhi padha jaata hai (prefix 'enc:v1:' na ho to as-is).
"""
import os
import logging

logger = logging.getLogger(__name__)
PREFIX = "enc:v1:"
_fernet = None
_warned = False


def _get():
    global _fernet, _warned
    if _fernet is not None:
        return _fernet
    key = os.environ.get("PANEL_ENC_KEY", "").strip()
    if not key:
        if not _warned:
            logger.warning("PANEL_ENC_KEY set nahi hai — bot env vars PLAINTEXT me save honge.")
            _warned = True
        return None
    try:
        from cryptography.fernet import Fernet
        _fernet = Fernet(key.encode())
    except Exception as e:
        raise RuntimeError(f"PANEL_ENC_KEY invalid: {e}")
    return _fernet


def enabled() -> bool:
    return _get() is not None


def encrypt(text: str | None) -> str | None:
    if text is None or str(text).startswith(PREFIX):
        return text
    f = _get()
    if f is None:
        return text
    return PREFIX + f.encrypt(str(text).encode()).decode()


def decrypt(text: str | None) -> str | None:
    if text is None or not str(text).startswith(PREFIX):
        return text
    f = _get()
    if f is None:
        raise RuntimeError("Data encrypted hai par PANEL_ENC_KEY set nahi hai.")
    return f.decrypt(str(text)[len(PREFIX):].encode()).decode()
