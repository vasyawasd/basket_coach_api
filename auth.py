import os
import re
import hmac
import hashlib
import secrets
import sqlite3
import time
import json
import bcrypt
from typing import Optional, Dict, Any, List

DB_PATH = os.path.realpath(
    os.path.join(os.path.dirname(__file__), "coach_database.sqlite3")
)
LEGACY_JSON_PATH = os.path.realpath(
    os.path.join(os.path.dirname(__file__), "users_db.json")
)

SESSION_TTL = 86400  # 24 hours in seconds


def get_db_connection() -> sqlite3.Connection:
    """Creates a thread-safe connection to the SQLite database with WAL mode."""
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db() -> None:
    """Initializes the database schema and migrates legacy data if present."""
    with get_db_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL COLLATE NOCASE,
                salt TEXT NOT NULL,
                hash TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS history (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                timestamp TEXT NOT NULL,
                payload TEXT NOT NULL,
                result TEXT NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
            CREATE INDEX IF NOT EXISTS idx_sessions_token_hash ON sessions(token_hash);
            CREATE INDEX IF NOT EXISTS idx_history_user_id ON history(user_id);
        """)

    # Auto-migrate legacy users_db.json if it exists
    _migrate_legacy_json()


def _migrate_legacy_json() -> None:
    """One-time safe migration from legacy users_db.json to SQLite."""
    if not os.path.exists(LEGACY_JSON_PATH):
        return
    try:
        with open(LEGACY_JSON_PATH, "r", encoding="utf-8") as f:
            legacy_data = json.load(f)

        with get_db_connection() as conn:
            for u_name, u_info in legacy_data.items():
                cur = conn.cursor()
                cur.execute("SELECT id FROM users WHERE username = ?", (u_name,))
                if not cur.fetchone():
                    salt = u_info.get("salt", "")
                    pwd_hash = u_info.get("hash", "")
                    created_at = time.time()
                    cur.execute(
                        "INSERT INTO users (username, salt, hash, created_at) VALUES (?, ?, ?, ?)",
                        (u_name, salt, pwd_hash, created_at)
                    )
                    user_id = cur.lastrowid
                    # Migrate history
                    for item in u_info.get("history", []):
                        item_id = item.get("id", f"migrated_{secrets.token_hex(6)}")
                        ts = item.get("timestamp", "")
                        payload_str = json.dumps(item.get("payload", {}), ensure_ascii=False)
                        result_str = json.dumps(item.get("apiResult", {}), ensure_ascii=False)
                        cur.execute(
                            "INSERT OR IGNORE INTO history (id, user_id, timestamp, payload, result, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                            (item_id, user_id, ts, payload_str, result_str, time.time())
                        )
        # Rename legacy file to .migrated so we don't repeat
        os.replace(LEGACY_JSON_PATH, f"{LEGACY_JSON_PATH}.migrated")
        print("[DB] Legacy users_db.json successfully migrated to SQLite.")
    except Exception as e:
        print(f"[DB] Note during legacy migration: {e}")


# Initialize tables at module import
init_db()


def _hash_token(token: str) -> str:
    """Hashes session token for secure DB storage (prevent plaintext token leaks)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_password_3stage(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    """
    3-Stage Security Password Hashing Pipeline:
    1. Cryptographic Per-User Salt: 16-byte random salt.
    2. Pre-hash: SHA-256 binary digest (32 bytes - bypasses bcrypt 72-byte truncation limit).
    3. Bcrypt: Adaptive key derivation with work factor rounds=12.
    """
    if salt is None:
        salt_hex = os.urandom(16).hex()
    else:
        salt_hex = salt

    prehash = hashlib.sha256(f"{salt_hex}:{password}".encode("utf-8")).digest()
    bcrypt_hash = bcrypt.hashpw(prehash, bcrypt.gensalt(rounds=12)).decode("utf-8")
    return salt_hex, bcrypt_hash


def verify_password(password: str, salt_hex: str, expected_hash_str: str) -> bool:
    """Verifies password using the 3-Stage Security Pipeline in constant time."""
    try:
        prehash = hashlib.sha256(f"{salt_hex}:{password}".encode("utf-8")).digest()
        if expected_hash_str.startswith("$2b$") or expected_hash_str.startswith("$2a$"):
            return bcrypt.checkpw(prehash, expected_hash_str.encode("utf-8"))

        key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), 100000)
        return hmac.compare_digest(key.hex(), expected_hash_str)
    except Exception as e:
        print(f"[AUTH] Password verification error: {e}")
        return False


def register_user(username: str, password: str) -> Dict[str, Any]:
    """Registers a new user with input sanitization and 3-stage Bcrypt password hashing."""
    clean_username = re.sub(r"[^\w\.-]", "", str(username)).strip()
    if len(clean_username) < 3 or len(clean_username) > 30:
        raise ValueError("Имя пользователя должно содержать от 3 до 30 символов.")

    if len(password) < 6 or len(password) > 100:
        raise ValueError("Пароль должен содержать от 6 до 100 символов.")

    salt_hex, bcrypt_hash = hash_password_3stage(password)
    now = time.time()

    with get_db_connection() as conn:
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO users (username, salt, hash, created_at) VALUES (?, ?, ?, ?)",
                (clean_username, salt_hex, bcrypt_hash, now)
            )
            user_id = cur.lastrowid
        except sqlite3.IntegrityError:
            raise ValueError("Пользователь с таким именем уже существует.")

        # Issue secure session token
        token = secrets.token_urlsafe(32)
        token_hash = _hash_token(token)
        cur.execute(
            "INSERT INTO sessions (token_hash, user_id, username, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (token_hash, user_id, clean_username, now, now + SESSION_TTL)
        )

    return {"username": clean_username, "token": token}


def login_user(username: str, password: str) -> Dict[str, Any]:
    """Authenticates a user and issues a secure persistent session token."""
    clean_username = str(username).strip()

    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, username, salt, hash FROM users WHERE username = ?", (clean_username,))
        row = cur.fetchone()

        if not row or not verify_password(password, row["salt"], row["hash"]):
            raise ValueError("Неверное имя пользователя или пароль.")

        user_id = row["id"]
        actual_username = row["username"]
        now = time.time()

        token = secrets.token_urlsafe(32)
        token_hash = _hash_token(token)

        # Cleanup expired sessions for this user
        cur.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))

        cur.execute(
            "INSERT INTO sessions (token_hash, user_id, username, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (token_hash, user_id, actual_username, now, now + SESSION_TTL)
        )

    return {"username": actual_username, "token": token}


def get_current_user(token: Optional[str]) -> Optional[str]:
    """Validates session token from DB and returns username if valid and not expired."""
    if not token or len(token) < 20:
        return None

    token_hash = _hash_token(token)
    now = time.time()

    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT username, expires_at FROM sessions WHERE token_hash = ?",
            (token_hash,)
        )
        row = cur.fetchone()
        if not row:
            return None

        if row["expires_at"] < now:
            cur.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
            return None

        return row["username"]


def get_user_history(username: str) -> List[Dict[str, Any]]:
    """Returns stored plan history for specified user in reverse chronological order."""
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT h.id, h.timestamp, h.payload, h.result
            FROM history h
            JOIN users u ON h.user_id = u.id
            WHERE u.username = ?
            ORDER BY h.created_at DESC
            LIMIT 30
            """,
            (username,)
        )
        rows = cur.fetchall()

        history_items = []
        for r in rows:
            try:
                history_items.append({
                    "id": r["id"],
                    "timestamp": r["timestamp"],
                    "payload": json.loads(r["payload"]),
                    "apiResult": json.loads(r["result"])
                })
            except Exception:
                continue
        return history_items


def add_user_history_item(username: str, item: Dict[str, Any]) -> None:
    """Appends a new generated plan item to user's history in SQLite."""
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE username = ?", (username,))
        row = cur.fetchone()
        if not row:
            return

        user_id = row["id"]
        item_id = item.get("id") or f"plan_{int(time.time() * 1000)}"
        ts = item.get("timestamp") or ""
        payload_str = json.dumps(item.get("payload", {}), ensure_ascii=False)
        result_str = json.dumps(item.get("apiResult", {}), ensure_ascii=False)

        cur.execute(
            "INSERT OR REPLACE INTO history (id, user_id, timestamp, payload, result, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (item_id, user_id, ts, payload_str, result_str, time.time())
        )

        # Retain only last 30 items per user
        cur.execute(
            """
            DELETE FROM history
            WHERE user_id = ? AND id NOT IN (
                SELECT id FROM history WHERE user_id = ? ORDER BY created_at DESC LIMIT 30
            )
            """,
            (user_id, user_id)
        )


def delete_user_history_item(username: str, item_id: str) -> bool:
    """Deletes a specific history item belonging to the user."""
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            DELETE FROM history
            WHERE id = ? AND user_id = (SELECT id FROM users WHERE username = ?)
            """,
            (item_id, username)
        )
        return cur.rowcount > 0


def clear_user_history(username: str) -> bool:
    """Clears all history items for a user."""
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            DELETE FROM history
            WHERE user_id = (SELECT id FROM users WHERE username = ?)
            """,
            (username,)
        )
        return True
