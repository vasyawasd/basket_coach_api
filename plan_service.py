import datetime
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import uuid
from typing import Any, Dict, Optional

from db import get_db_connection
from rag import get_relevant_knowledge, sanitize_input
from llm_service import call_llm_api
from metrics import log_event
import auth

TASK_TTL_SECONDS = 3600  # finished/abandoned tasks are purged after 1 hour

# Daily generation quotas (rolling 24h). Protect the LLM API budget:
# anonymous users are capped hard, registered accounts get a higher tier.
ANON_DAILY_LIMIT = int(os.environ.get("ANON_DAILY_LIMIT", "3"))
USER_DAILY_LIMIT = int(os.environ.get("USER_DAILY_LIMIT", "15"))

# Field length caps shared by both Flask and FastAPI entrypoints
MAX_POSITION_LEN = 50
MAX_INJURIES_LEN = 500
MAX_MODEL_LEN = 50
MAX_GOAL_ITEM_LEN = 200
MAX_GOAL_TOTAL_LEN = 600
MAX_FEEDBACK_LEN = 600


def validate_plan_params(params: Dict[str, Any]) -> Optional[str]:
    """Validates raw plan params; returns a Russian error message or None if valid."""
    try:
        height = int(params.get("height", 0))
        weight = float(params.get("weight", 0))
        days_per_week = int(params.get("days_per_week", 0))
    except (ValueError, TypeError):
        return "Параметры должны быть числами."

    if not (50 < height < 300) or not (30 < weight < 300) or not (1 <= days_per_week <= 7):
        return "Некорректные параметры игрока."

    position = params.get("position", "")
    if not isinstance(position, str) or len(position) > MAX_POSITION_LEN:
        return "Некорректная позиция игрока."

    injuries = params.get("injuries") or "None"
    if not isinstance(injuries, str) or len(injuries) > MAX_INJURIES_LEN:
        return "Слишком длинное описание травм (максимум 500 символов)."

    model = params.get("model") or "auto"
    if not isinstance(model, str) or len(model) > MAX_MODEL_LEN:
        return "Некорректное имя модели."

    goal = params.get("goal", "")
    if isinstance(goal, list):
        if not goal or not all(isinstance(g, str) for g in goal):
            return "Некорректно указаны цели тренировки."
        if any(len(g) > MAX_GOAL_ITEM_LEN for g in goal):
            return "Слишком длинная цель тренировки (максимум 200 символов на цель)."
        if sum(len(g) for g in goal) > MAX_GOAL_TOTAL_LEN:
            return "Слишком много целей тренировки."
    elif not isinstance(goal, str) or not (1 <= len(goal) <= MAX_GOAL_ITEM_LEN):
        return "Некорректно указана цель тренировки."

    feedback = params.get("feedback")
    if feedback is not None and (not isinstance(feedback, str) or len(feedback) > MAX_FEEDBACK_LEN):
        return "Слишком длинный фидбек (максимум 600 символов)."

    return None


def check_daily_quota(username: Optional[str], client_ip: Optional[str]) -> Optional[str]:
    """Rolling 24h generation quota; returns a Russian error message or None."""
    day_ago = time.time() - 86400
    with get_db_connection() as conn:
        if username:
            used = conn.execute(
                "SELECT COUNT(*) FROM plan_tasks WHERE owner = ? AND created_at > ?",
                (username, day_ago)
            ).fetchone()[0]
            if used >= USER_DAILY_LIMIT:
                return f"Дневной лимит генераций ({USER_DAILY_LIMIT} в сутки) исчерпан. Попробуйте завтра."
        elif client_ip:
            used = conn.execute(
                "SELECT COUNT(*) FROM plan_tasks WHERE client_ip = ? AND owner IS NULL AND created_at > ?",
                (client_ip, day_ago)
            ).fetchone()[0]
            if used >= ANON_DAILY_LIMIT:
                return (
                    f"Дневной лимит бесплатных генераций ({ANON_DAILY_LIMIT} в сутки) исчерпан. "
                    "Зарегистрируйтесь, чтобы получать больше планов, или попробуйте завтра."
                )
    return None

SYSTEM_PROMPT = (
    "Ты — элитный баскетбольный тренер по физической и технической подготовке. "
    "Составь подробную, безопасную и научно обоснованную программу тренировок на русском языке. "
    "ВНИМАНИЕ: Верни результат СТРОГО в формате JSON со следующей структурой:\n"
    "{\n"
    '  "summary": "Краткое резюме фокуса программы",\n'
    '  "safety_notes": ["Правило техники 1", "Правило 2"],\n'
    '  "schedule": [\n'
    "    {\n"
    '      "day": "День 1",\n'
    '      "focus": "Фокус тренировки (например: Взрывная сила и плиометрика)",\n'
    '      "exercises": [\n'
    "        {\n"
    '          "name": "Название упражнения",\n'
    '          "sets": "3-4",\n'
    '          "reps": "6-8 повторений или 30 сек",\n'
    '          "notes": "Указания по технике"\n'
    "        }\n"
    "      ]\n"
    "    }\n"
    "  ]\n"
    "}"
)


def _clean_fields(params: Dict[str, Any]) -> Dict[str, str]:
    """Sanitizes free-text player parameters from a raw params dict."""
    clean_position = sanitize_input(params.get("position", ""))
    goal_val = params.get("goal", "")
    if isinstance(goal_val, list):
        clean_goal = ", ".join([sanitize_input(g) for g in goal_val])
    else:
        clean_goal = sanitize_input(goal_val)
    clean_injuries = sanitize_input(params.get("injuries") or "None")
    return {
        "position": clean_position,
        "goal": clean_goal,
        "injuries": clean_injuries,
    }


def build_user_prompt(params: Dict[str, Any], clean: Dict[str, str]) -> str:
    return (
        f"Player parameters:\n"
        f"- Height: {params.get('height')} cm\n"
        f"- Weight: {params.get('weight')} kg\n"
        f"- Position: {clean['position']}\n"
        f"- Goal: {clean['goal']}\n"
        f"- Days per week: {params.get('days_per_week')}\n"
        f"- Injuries/Limitations: {clean['injuries']}"
    )


def _hash_poll_token(poll_token: str) -> str:
    return hashlib.sha256(poll_token.encode("utf-8")).hexdigest()


def create_task(params: Dict[str, Any], username: Optional[str] = None, client_ip: Optional[str] = None) -> Dict[str, str]:
    """
    Persists a new plan task in SQLite and starts background processing.
    Returns the task_id plus a one-time poll_token that grants polling access
    (for anonymous tasks the token is the only way to read the status).
    """
    task_id = str(uuid.uuid4())
    poll_token = secrets.token_urlsafe(24)
    now = time.time()

    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM plan_tasks WHERE created_at < ?", (now - TASK_TTL_SECONDS,))
        cur.execute(
            "INSERT INTO plan_tasks (task_id, status, owner, poll_token_hash, result, client_ip, created_at) "
            "VALUES (?, 'processing', ?, ?, NULL, ?, ?)",
            (task_id, username, _hash_poll_token(poll_token), client_ip, now)
        )

    log_event("plan_requested", subject=username or client_ip,
              meta={"model": params.get("model") or "auto", "ip": client_ip,
                    "adapted": bool(params.get("feedback"))})

    threading.Thread(target=process_plan_task, args=(task_id, params, username), daemon=True).start()
    return {"task_id": task_id, "poll_token": poll_token}


def _save_task_result(task_id: str, payload: Dict[str, Any]) -> None:
    with get_db_connection() as conn:
        conn.execute(
            "UPDATE plan_tasks SET status = ?, result = ? WHERE task_id = ?",
            (payload["status"], json.dumps(payload, ensure_ascii=False), task_id)
        )


def get_task(task_id: str, username: Optional[str] = None, poll_token: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Returns the task payload if the caller is authorized: either the authenticated
    owner of the task, or anyone holding the task's poll_token.
    """
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT status, owner, poll_token_hash, result FROM plan_tasks WHERE task_id = ?",
            (task_id,)
        ).fetchone()

    if not row:
        return None

    authorized = False
    if row["owner"] and username and row["owner"] == username:
        authorized = True
    if row["poll_token_hash"] and poll_token:
        if hmac.compare_digest(row["poll_token_hash"], _hash_poll_token(poll_token)):
            authorized = True

    if not authorized:
        return None

    if row["status"] == "processing":
        return {"status": "processing"}

    try:
        return json.loads(row["result"])
    except Exception:
        return {"status": "error", "message": "Task result is corrupted"}


def process_plan_task(task_id: str, params: Dict[str, Any], username: Optional[str] = None):
    """Background task to process RAG and LLM generation (runs in a separate thread)."""
    started = time.time()
    try:
        model_name = params.get("model") or "auto"
        print(f"[TASK {task_id[:8]}] Starting plan generation (cascade mode: '{model_name}')...", flush=True)

        clean = _clean_fields(params)
        rag_context = get_relevant_knowledge(
            goal=clean["goal"],
            injuries=clean["injuries"],
            position=clean["position"],
        )

        user_prompt = build_user_prompt(params, clean)
        feedback = sanitize_input(params.get("feedback") or "")
        if feedback:
            user_prompt += (
                f"\n\nATHLETE FEEDBACK FROM THE PREVIOUS WEEK:\n- {feedback}\n"
                "Use this feedback to adapt the program (adjust volume, intensity, "
                "exercise selection)."
            )

        print(f"[TASK {task_id[:8]}] RAG context ready ({len(rag_context)} chars), calling LLM ({model_name})...", flush=True)
        llm_result = call_llm_api(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            context_text=rag_context,
            selected_model=model_name,
        )
        usage = llm_result.pop("_usage", {})
        print(f"[TASK {task_id[:8]}] LLM returned, source: {llm_result.get('source')}", flush=True)

        result_payload = {
            "status": "success",
            "owner": username,
            "source": llm_result.get("source"),
            "rag_context_snippet": rag_context[:300] + ("..." if len(rag_context) > 300 else ""),
            "data": llm_result.get("data"),
            "adapted": bool(feedback),
        }

        _save_task_result(task_id, result_payload)
        log_event("plan_completed", subject=username, meta={
            "source": llm_result.get("source"),
            "duration_s": round(time.time() - started, 1),
            "tokens_prompt": usage.get("prompt_tokens", 0),
            "tokens_completion": usage.get("completion_tokens", 0),
            "adapted": bool(feedback),
        })

        # If user is authenticated, automatically persist to backend user history
        if username:
            now_str = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
            history_item = {
                "id": f"plan_{int(datetime.datetime.now().timestamp() * 1000)}",
                "timestamp": now_str,
                "payload": params,
                "apiResult": result_payload
            }
            auth.add_user_history_item(username, history_item)

    except Exception as e:
        # Full details stay in server logs; the client gets a generic message
        print(f"[TASK {task_id[:8]}] FAILED: {type(e).__name__}: {e}", flush=True)
        _save_task_result(task_id, {
            "status": "error",
            "owner": username,
            "message": "Внутренняя ошибка при генерации плана. Попробуйте позже."
        })
        log_event("plan_failed", subject=username, meta={
            "error_type": type(e).__name__,
            "duration_s": round(time.time() - started, 1),
        })
