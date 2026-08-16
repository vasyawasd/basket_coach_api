import os
import sqlite3

DB_PATH = os.path.realpath(
    os.path.join(os.path.dirname(__file__), "coach_database.sqlite3")
)


def get_db_connection() -> sqlite3.Connection:
    """Creates a thread-safe connection to the SQLite database with WAL mode."""
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db() -> None:
    """Initializes the database schema shared by auth, plan tasks and rate limiting."""
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

            CREATE TABLE IF NOT EXISTS plan_tasks (
                task_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                owner TEXT,
                poll_token_hash TEXT,
                result TEXT,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS rate_limit_hits (
                ip TEXT NOT NULL,
                kind TEXT NOT NULL,
                ts REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
            CREATE INDEX IF NOT EXISTS idx_sessions_token_hash ON sessions(token_hash);
            CREATE INDEX IF NOT EXISTS idx_history_user_id ON history(user_id);
            CREATE INDEX IF NOT EXISTS idx_plan_tasks_created_at ON plan_tasks(created_at);
            CREATE INDEX IF NOT EXISTS idx_rate_limit_lookup ON rate_limit_hits(ip, kind, ts);
        """)


# Initialize tables at module import
init_db()
