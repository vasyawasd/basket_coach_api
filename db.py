from contextlib import contextmanager
import os
import sqlite3

DB_PATH = os.path.realpath(
    os.environ.get("COACH_DB_PATH")
    or os.path.join(os.path.dirname(__file__), "coach_database.sqlite3")
)


@contextmanager
def get_db_connection():
    """Creates a thread-safe connection to the SQLite database with WAL mode and automatic cleanup."""
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    """Initializes the database schema shared by auth, plan tasks and rate limiting."""
    with get_db_connection() as conn:
        # Migration for databases created before client_ip tracking existed
        # (must run before the index statements below reference the column)
        columns = [row[1] for row in conn.execute("PRAGMA table_info(plan_tasks)")]
        if columns and "client_ip" not in columns:
            conn.execute("ALTER TABLE plan_tasks ADD COLUMN client_ip TEXT")

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
                client_ip TEXT,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                subject TEXT,
                meta TEXT,
                ts REAL NOT NULL
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
            CREATE INDEX IF NOT EXISTS idx_plan_tasks_ip ON plan_tasks(client_ip, created_at);
            CREATE INDEX IF NOT EXISTS idx_plan_tasks_owner ON plan_tasks(owner, created_at);
            CREATE INDEX IF NOT EXISTS idx_events_kind_ts ON events(kind, ts);
            CREATE INDEX IF NOT EXISTS idx_rate_limit_lookup ON rate_limit_hits(ip, kind, ts);
        """)


# Initialize tables at module import
init_db()

