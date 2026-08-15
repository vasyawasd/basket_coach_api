import os
import uuid
import threading
import time
import datetime
from typing import Optional, Dict, Any, List, Union
from flask import Flask, request, jsonify, send_file, make_response
from flask_cors import CORS

from rag import get_relevant_knowledge, sanitize_input
from llm_service import call_llm_api
import auth

app = Flask(__name__, static_folder=None)

# ponytail: VULN-004 fix — restrict CORS to dev / local ports
CORS(
    app,
    resources={r"/*": {"origins": [
        "http://localhost:8000", "http://127.0.0.1:8000",
        "http://localhost:5000", "http://127.0.0.1:5000"
    ]}},
    supports_credentials=True,
    methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"]
)

# In-memory task database for polling
tasks_db: Dict[str, Dict[str, Any]] = {}

# VULN-005 fix: simple in-memory rate limiter for auth endpoints
_rate_limit: Dict[str, list] = {}
RATE_LIMIT_WINDOW = 60   # seconds
RATE_LIMIT_MAX = 10      # max attempts per window per IP


def rate_limit_check() -> Optional[tuple]:
    """Checks whether the client IP has exceeded the auth rate limit."""
    ip = request.remote_addr or "unknown"
    now = time.time()
    attempts = [t for t in _rate_limit.get(ip, []) if t > now - RATE_LIMIT_WINDOW]
    if len(attempts) >= RATE_LIMIT_MAX:
        return jsonify({"detail": "Слишком много запросов. Попробуйте позже."}), 429
    attempts.append(now)
    _rate_limit[ip] = attempts
    return None


def get_token_user() -> Optional[str]:
    """Helper to extract and verify username from Authorization header."""
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return None
    token = auth_header.split(" ", 1)[1].strip()
    return auth.get_current_user(token)


@app.route("/", methods=["GET"])
def root():
    return send_file("index.html")


# Auth Endpoints
@app.route("/api/register", methods=["POST"])
def register():
    rl_error = rate_limit_check()
    if rl_error:
        return rl_error

    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")

    if len(username) < 3 or len(username) > 30:
        return jsonify({"detail": "Имя пользователя должно быть от 3 до 30 символов."}), 400
    if len(password) < 6 or len(password) > 100:
        return jsonify({"detail": "Пароль должен быть от 6 до 100 символов."}), 400

    try:
        res = auth.register_user(username, password)
        return jsonify({"status": "success", "username": res["username"], "token": res["token"]})
    except ValueError as e:
        return jsonify({"detail": str(e)}), 400


@app.route("/api/login", methods=["POST"])
def login():
    rl_error = rate_limit_check()
    if rl_error:
        return rl_error

    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")

    try:
        res = auth.login_user(username, password)
        return jsonify({"status": "success", "username": res["username"], "token": res["token"]})
    except ValueError as e:
        return jsonify({"detail": str(e)}), 401


@app.route("/api/me", methods=["GET"])
def get_me():
    username = get_token_user()
    if not username:
        return jsonify({"authenticated": False})
    return jsonify({"authenticated": True, "username": username})


@app.route("/api/history", methods=["GET"])
def user_history():
    username = get_token_user()
    if not username:
        return jsonify({"detail": "Требуется авторизация."}), 401
    return jsonify({"status": "success", "history": auth.get_user_history(username)})


@app.route("/api/history/<item_id>", methods=["DELETE"])
def delete_history_item(item_id: str):
    username = get_token_user()
    if not username:
        return jsonify({"detail": "Требуется авторизация."}), 401
    success = auth.delete_user_history_item(username, item_id)
    return jsonify({"status": "success" if success else "error"})


@app.route("/api/history", methods=["DELETE"])
def clear_history():
    username = get_token_user()
    if not username:
        return jsonify({"detail": "Требуется авторизация."}), 401
    auth.clear_user_history(username)
    return jsonify({"status": "success"})


def process_plan_task(task_id: str, params: dict, username: Optional[str] = None):
    """Background task to process RAG and LLM generation (runs in a separate thread)."""
    try:
        model_name = params.get("model") or "auto"
        print(f"[TASK {task_id[:8]}] Starting plan generation (cascade mode: '{model_name}')...")

        clean_position = sanitize_input(params.get("position", ""))
        goal_val = params.get("goal", "")
        if isinstance(goal_val, list):
            clean_goal = ", ".join([sanitize_input(g) for g in goal_val])
        else:
            clean_goal = sanitize_input(goal_val)
        clean_injuries = sanitize_input(params.get("injuries") or "None")

        rag_context = get_relevant_knowledge(
            goal=clean_goal,
            injuries=clean_injuries,
            position=clean_position,
        )

        system_prompt = (
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
            '        {\n'
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

        user_prompt = (
            f"Player parameters:\n"
            f"- Height: {params.get('height')} cm\n"
            f"- Weight: {params.get('weight')} kg\n"
            f"- Position: {clean_position}\n"
            f"- Goal: {clean_goal}\n"
            f"- Days per week: {params.get('days_per_week')}\n"
            f"- Injuries/Limitations: {clean_injuries}"
        )

        print(f"[TASK {task_id[:8]}] RAG context ready ({len(rag_context)} chars), calling LLM ({model_name})...")
        llm_result = call_llm_api(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            context_text=rag_context,
            selected_model=model_name,
        )
        print(f"[TASK {task_id[:8]}] LLM returned, source: {llm_result.get('source')}")

        result_payload = {
            "status": "success",
            "owner": username,
            "source": llm_result.get("source"),
            "rag_context_snippet": rag_context[:300] + ("..." if len(rag_context) > 300 else ""),
            "data": llm_result.get("data"),
        }

        tasks_db[task_id] = result_payload

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
        print(f"[TASK {task_id[:8]}] FAILED: {type(e).__name__}: {e}")
        tasks_db[task_id] = {
            "status": "error",
            "owner": username,
            "message": str(e)
        }


@app.route("/generate_plan", methods=["POST"])
def generate_plan():
    username = get_token_user()
    data = request.get_json(silent=True) or {}

    # Validation
    try:
        height = int(data.get("height", 0))
        weight = float(data.get("weight", 0))
        days_per_week = int(data.get("days_per_week", 0))
        if not (50 < height < 300) or not (30 < weight < 300) or not (1 <= days_per_week <= 7):
            return jsonify({"detail": "Некорректные параметры игрока."}), 400
    except (ValueError, TypeError):
        return jsonify({"detail": "Параметры должны быть числами."}), 400

    task_id = str(uuid.uuid4())
    tasks_db[task_id] = {"status": "processing", "created_at": time.time(), "owner": username}

    # VULN-003 fix: cleanup tasks older than 1 hour
    now = time.time()
    expired = [t for t, d in tasks_db.items() if d.get("created_at", 0) < now - 3600]
    for t in expired:
        del tasks_db[t]

    threading.Thread(target=process_plan_task, args=(task_id, data, username), daemon=True).start()
    return jsonify({"status": "processing", "task_id": task_id})


@app.route("/plan_status/<task_id>", methods=["GET"])
def get_plan_status(task_id: str):
    username = get_token_user()
    task = tasks_db.get(task_id)
    if not task:
        return jsonify({"status": "error", "message": "Task not found"})
    # VULN-007 fix: if task has owner, only owner can view
    if task.get("owner") and task["owner"] != username:
        return jsonify({"status": "error", "message": "Task not found"})
    return jsonify(task)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"[*] Basketball Coach Flask API running on http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
