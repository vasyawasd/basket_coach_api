import json
import os
import re
import hmac
import hashlib
import secrets
import threading
import time
import bcrypt
from typing import Optional, Dict, Any, List

USERS_DB_PATH = os.path.realpath(
    os.path.join(os.path.dirname(__file__), "users_db.json")
)

# In-memory storage for active sessions (token -> (username, created_at))
SESSIONS: Dict[str, tuple] = {}
SESSION_TTL = 86400  # ponytail: 24h session expiry
_db_lock = threading.Lock()  # VULN-002 fix: prevents race condition on file writes


def _load_users() -> Dict[str, Dict[str, Any]]:
    """Loads users database safely from users_db.json (thread-safe)."""
    with _db_lock:
        if not os.path.exists(USERS_DB_PATH):
            return {}
        try:
            with open(USERS_DB_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}


def _save_users(users: Dict[str, Dict[str, Any]]) -> None:
    """Saves users database with atomic write (tmp + os.replace) under lock."""
    with _db_lock:
        try:
            tmp_path = f"{USERS_DB_PATH}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(users, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, USERS_DB_PATH)
        except Exception as e:
            print(f"[AUTH] Failed to save users DB: {e}")


def hash_password_3stage(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    """
    3-Stage Security Password Hashing Pipeline:
    1. Cryptographic Per-User Salt: 16-byte random salt.
    2. Pre-hash: SHA-256 binary digest (32 bytes - bypasses bcrypt 72-byte truncation limit).
    3. Bcrypt: Adaptive key derivation with work factor rounds=12.

    Returns (salt_hex, bcrypt_hash_str).
    """
    if salt is None:
        salt_hex = os.urandom(16).hex()
    else:
        salt_hex = salt

    # Stage 1 & 2: Salted SHA-256 pre-hashing (exactly 32 bytes binary digest)
    prehash = hashlib.sha256(f"{salt_hex}:{password}".encode('utf-8')).digest()

    # Stage 3: Bcrypt adaptive key derivation (rounds=12)
    bcrypt_hash = bcrypt.hashpw(prehash, bcrypt.gensalt(rounds=12)).decode('utf-8')

    return salt_hex, bcrypt_hash


def verify_password(password: str, salt_hex: str, expected_hash_str: str) -> bool:
    """
    Verifies password using the 3-Stage Security Pipeline.
    Supports constant-time Bcrypt hash comparison.
    """
    try:
        # Stage 1 & 2: Salted SHA-256 pre-hash
        prehash = hashlib.sha256(f"{salt_hex}:{password}".encode('utf-8')).digest()

        # Stage 3: Bcrypt verification
        if expected_hash_str.startswith("$2b$") or expected_hash_str.startswith("$2a$"):
            return bcrypt.checkpw(prehash, expected_hash_str.encode('utf-8'))

        # Fallback check for legacy testing hashes if present
        key = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt_hex), 100000)
        return hmac.compare_digest(key.hex(), expected_hash_str)
    except Exception as e:
        print(f"[AUTH] Password verification error: {e}")
        return False


def register_user(username: str, password: str) -> Dict[str, Any]:
    """
    Registers a new user with input sanitization and 3-stage Bcrypt password hashing.
    """
    clean_username = re.sub(r"[^\w\.-]", "", str(username)).strip()
    if len(clean_username) < 3 or len(clean_username) > 30:
        raise ValueError("Имя пользователя должно содержать от 3 до 30 символов.")

    if len(password) < 6:
        raise ValueError("Пароль должен содержать не менее 6 символов.")

    users = _load_users()
    if clean_username.lower() in [u.lower() for u in users.keys()]:
        raise ValueError("Пользователь с таким именем уже существует.")

    salt_hex, bcrypt_hash = hash_password_3stage(password)

    users[clean_username] = {
        "username": clean_username,
        "salt": salt_hex,
        "hash": bcrypt_hash,
        "algo": "sha256+salt+bcrypt",
        "history": []
    }
    _save_users(users)

    # Generate session token
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = (clean_username, time.time())

    return {"username": clean_username, "token": token}


def login_user(username: str, password: str) -> Dict[str, Any]:
    """
    Authenticates a user and returns a session token.
    """
    clean_username = str(username).strip()
    users = _load_users()

    user_entry = None
    for u_key, u_data in users.items():
        if u_key.lower() == clean_username.lower():
            user_entry = u_data
            break

    if not user_entry:
        raise ValueError("Неверное имя пользователя или пароль.")

    if not verify_password(password, user_entry["salt"], user_entry["hash"]):
        raise ValueError("Неверное имя пользователя или пароль.")

    token = secrets.token_urlsafe(32)
    SESSIONS[token] = (user_entry["username"], time.time())

    return {"username": user_entry["username"], "token": token}


def get_current_user(token: Optional[str]) -> Optional[str]:
    """Returns username if session token is valid and not expired (24h TTL)."""
    if not token:
        return None
    session = SESSIONS.get(token)
    if not session:
        return None
    username, created_at = session
    if time.time() - created_at > SESSION_TTL:
        del SESSIONS[token]  # ponytail: expired session cleanup
        return None
    return username


def get_user_history(username: str) -> List[Dict[str, Any]]:
    """Returns stored plan history for specified user."""
    users = _load_users()
    user_entry = users.get(username)
    if not user_entry:
        return []
    return user_entry.get("history", [])


def add_user_history_item(username: str, item: Dict[str, Any]) -> None:
    """Appends a new generated plan item to user's history."""
    users = _load_users()
    if username in users:
        if "history" not in users[username]:
            users[username]["history"] = []
        users[username]["history"].insert(0, item)
        # Limit history to 30 items per user
        if len(users[username]["history"]) > 30:
            users[username]["history"].pop()
        _save_users(users)


def delete_user_history_item(username: str, item_id: str) -> bool:
    """Deletes a specific history item for a user."""
    users = _load_users()
    if username in users and "history" in users[username]:
        users[username]["history"] = [
            i for i in users[username]["history"] if i.get("id") != item_id
        ]
        _save_users(users)
        return True
    return False


def clear_user_history(username: str) -> bool:
    """Clears all history items for a user."""
    users = _load_users()
    if username in users:
        users[username]["history"] = []
        _save_users(users)
        return True
    return False
