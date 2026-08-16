import random
import time

from db import get_db_connection

# Rows older than this are considered stale regardless of the caller's window
HARD_RETENTION_SECONDS = 7200


def is_rate_limited(ip: str, kind: str, max_attempts: int, window_seconds: int) -> bool:
    """
    Sliding-window rate limiter backed by SQLite.
    Survives server restarts and works across multiple workers, unlike an in-memory dict.
    """
    now = time.time()
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM rate_limit_hits WHERE ip = ? AND kind = ? AND ts > ?",
            (ip, kind, now - window_seconds)
        )
        recent_count = cur.fetchone()[0]

        if recent_count >= max_attempts:
            return True

        cur.execute(
            "INSERT INTO rate_limit_hits (ip, kind, ts) VALUES (?, ?, ?)",
            (ip, kind, now)
        )

        # Opportunistic purge so the table does not grow forever
        if random.random() < 0.02:
            cur.execute(
                "DELETE FROM rate_limit_hits WHERE ts < ?",
                (now - HARD_RETENTION_SECONDS,)
            )

    return False
