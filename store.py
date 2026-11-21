"""Account persistence.

Accounts are stored in data.json next to the app. Cookies are DPAPI-encrypted
(tied to this Windows user) and stored base64-encoded. Avatar images are
cached under avatars/.
"""

import base64
import json
import os
import random
import sys
import threading

import crypto

# Frozen (PyInstaller) builds live in a temp dir — keep data next to the exe.
if getattr(sys, "frozen", False):
    DATA_DIR = os.path.dirname(sys.executable)
else:
    DATA_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(DATA_DIR, "data.json")
AVATAR_DIR = os.path.join(DATA_DIR, "avatars")

_lock = threading.Lock()


def _load() -> dict:
    if not os.path.exists(DATA_FILE):
        return {"accounts": []}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"accounts": []}


def _save(data: dict) -> None:
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, DATA_FILE)


def list_accounts() -> list[dict]:
    """Return accounts with decrypted cookies."""
    with _lock:
        accounts = _load().get("accounts", [])
        for acc in accounts:
            acc["cookie_value"] = _decrypt(acc.get("cookie", ""))
        return accounts


def add_account(user_id: int, username: str, display_name: str, cookie: str) -> None:
    with _lock:
        data = _load()
        data["accounts"] = [a for a in data["accounts"] if a["user_id"] != user_id]
        data["accounts"].append(
            {
                "user_id": user_id,
                "username": username,
                "display_name": display_name,
                "cookie": base64.b64encode(crypto.protect(cookie.encode())).decode(),
            }
        )
        _save(data)


def remove_account(user_id: int) -> None:
    with _lock:
        data = _load()
        data["accounts"] = [a for a in data["accounts"] if a["user_id"] != user_id]
        _save(data)
    avatar = os.path.join(AVATAR_DIR, f"{user_id}.png")
    if os.path.exists(avatar):
        try:
            os.remove(avatar)
        except OSError:
            pass


def ensure_btid(user_id: int) -> str:
    """Persistent browserTrackerId per account — used as the -b launch arg so
    we can identify (and kill) that account's running clients later."""
    with _lock:
        data = _load()
        for a in data["accounts"]:
            if a["user_id"] == user_id:
                if not a.get("btid"):
                    a["btid"] = "".join(str(random.randint(0, 9)) for _ in range(12))
                    _save(data)
                return a["btid"]
    return ""


def set_account_flag(user_id: int, key: str, value) -> None:
    """Set a per-account flag (keep_active, no_trim, ...)."""
    with _lock:
        data = _load()
        for a in data["accounts"]:
            if a["user_id"] == user_id:
                a[key] = value
                _save(data)
                return


def get_setting(key: str, default=None):
    with _lock:
        return _load().get("settings", {}).get(key, default)


def set_setting(key: str, value) -> None:
    with _lock:
        data = _load()
        data.setdefault("settings", {})[key] = value
        _save(data)


def avatar_path(user_id: int) -> str:
    os.makedirs(AVATAR_DIR, exist_ok=True)
    return os.path.join(AVATAR_DIR, f"{user_id}.png")


def _decrypt(b64: str) -> str:
    try:
        return crypto.unprotect(base64.b64decode(b64)).decode()
    except Exception:
        return ""
