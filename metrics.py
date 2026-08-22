import json
import time
from typing import Any, Dict, Optional

from db import get_db_connection


def log_event(kind: str, subject: Optional[str] = None, meta: Optional[Dict[str, Any]] = None) -> None:
    """Records a product event (register, plan_generated, ...) for analytics."""
    try:
        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO events (kind, subject, meta, ts) VALUES (?, ?, ?, ?)",
                (kind, subject, json.dumps(meta or {}, ensure_ascii=False), time.time())
            )
    except Exception as e:
        # Metrics must never break user-facing flows
        print(f"[METRICS] Failed to log event '{kind}': {e}", flush=True)


def get_stats() -> Dict[str, Any]:
    """Aggregated product metrics for the admin dashboard."""
    now = time.time()
    day_ago = now - 86400

    with get_db_connection() as conn:
        users_total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        plans_total = conn.execute("SELECT COUNT(*) FROM plan_tasks").fetchone()[0]
        plans_today = conn.execute(
            "SELECT COUNT(*) FROM plan_tasks WHERE created_at > ?", (day_ago,)
        ).fetchone()[0]
        plans_failed = conn.execute(
            "SELECT COUNT(*) FROM plan_tasks WHERE status = 'error'"
        ).fetchone()[0]
        anon_today = conn.execute(
            "SELECT COUNT(*) FROM plan_tasks WHERE created_at > ? AND owner IS NULL",
            (day_ago,)
        ).fetchone()[0]

        # Token usage & duration from plan_completed events
        completed = conn.execute(
            "SELECT meta FROM events WHERE kind = 'plan_completed' AND ts > ?",
            (now - 30 * 86400,)
        ).fetchall()
        tokens_prompt = tokens_completion = 0
        durations = []
        sources: Dict[str, int] = {}
        for row in completed:
            try:
                meta = json.loads(row["meta"])
            except Exception:
                continue
            tokens_prompt += int(meta.get("tokens_prompt") or 0)
            tokens_completion += int(meta.get("tokens_completion") or 0)
            if meta.get("duration_s"):
                durations.append(float(meta["duration_s"]))
            if meta.get("source"):
                sources[meta["source"]] = sources.get(meta["source"], 0) + 1

        # Generations per day for the last 14 days
        daily_rows = conn.execute(
            """
            SELECT date(created_at, 'unixepoch', 'localtime') AS day, COUNT(*) AS cnt
            FROM plan_tasks
            WHERE created_at > ?
            GROUP BY day ORDER BY day
            """,
            (now - 14 * 86400,)
        ).fetchall()

    completed_count = len(completed)
    return {
        "users_total": users_total,
        "plans_total": plans_total,
        "plans_today": plans_today,
        "plans_failed_total": plans_failed,
        "anonymous_share_today": round(anon_today / plans_today, 2) if plans_today else 0,
        "avg_generation_seconds": round(sum(durations) / len(durations), 1) if durations else None,
        "tokens_last_30d": {
            "prompt": tokens_prompt,
            "completion": tokens_completion,
            "total": tokens_prompt + tokens_completion,
        },
        "models_last_30d": sources,
        "generations_per_day": [{"date": r["day"], "plans": r["cnt"]} for r in daily_rows],
    }

