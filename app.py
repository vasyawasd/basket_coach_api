import os
from typing import Optional

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

import auth
import plan_service
from rate_limiter import is_rate_limited

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # 2MB max request body to prevent memory exhaustion

# Restrict CORS to dev / local ports
CORS(
    app,
    resources={r"/*": {"origins": [
        "http://localhost:8000", "http://127.0.0.1:8000",
        "http://localhost:5000", "http://127.0.0.1:5000"
    ]}},
    supports_credentials=True,
    methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Poll-Token"]
)


@app.after_request
def set_security_headers(response):
    """Injects standard HTTP security headers to all responses."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self' http://localhost:8000 http://127.0.0.1:8000 http://localhost:5000 http://127.0.0.1:5000;"
    )
    return response


# Rate limit policies (sliding window, backed by SQLite: survives restarts, multi-worker safe)
AUTH_RATE_LIMIT = (10, 60)       # 10 auth attempts per minute per IP
GEN_RATE_LIMIT = (30, 3600)      # 30 plan generations per hour per IP
POLL_RATE_LIMIT = (120, 60)      # 120 status polls per minute per IP (frontend polls every 3s)


def _auth_rate_limited() -> Optional[tuple]:
    max_attempts, window = AUTH_RATE_LIMIT
    if is_rate_limited(request.remote_addr or "unknown", "auth", max_attempts, window):
        return jsonify({"detail": "Слишком много попыток входа/регистрации. Попробуйте позже."}), 429
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
    rl_error = _auth_rate_limited()
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
    rl_error = _auth_rate_limited()
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


@app.route("/api/logout", methods=["POST"])
def logout():
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1].strip()
        auth.logout_user(token)
    # Idempotent: always success, even with an invalid or missing token
    return jsonify({"status": "success"})


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


@app.route("/generate_plan", methods=["POST"])
def generate_plan():
    max_attempts, window = GEN_RATE_LIMIT
    if is_rate_limited(request.remote_addr or "unknown", "generate", max_attempts, window):
        return jsonify({"detail": "Превышен лимит генераций планов (максимум 30 в час). Попробуйте позже."}), 429

    username = get_token_user()
    data = request.get_json(silent=True) or {}

    # Validation (shared rules with the FastAPI app)
    error = plan_service.validate_plan_params(data)
    if error:
        return jsonify({"detail": error}), 400

    created = plan_service.create_task(data, username)
    return jsonify({
        "status": "processing",
        "task_id": created["task_id"],
        "poll_token": created["poll_token"],
    })


@app.route("/plan_status/<task_id>", methods=["GET"])
def get_plan_status(task_id: str):
    max_attempts, window = POLL_RATE_LIMIT
    if is_rate_limited(request.remote_addr or "unknown", "poll", max_attempts, window):
        return jsonify({"detail": "Слишком много запросов статуса. Попробуйте позже."}), 429

    username = get_token_user()
    poll_token = request.headers.get("X-Poll-Token") or request.args.get("poll_token")
    task = plan_service.get_task(task_id, username=username, poll_token=poll_token)
    if not task:
        return jsonify({"status": "error", "message": "Task not found"})
    return jsonify(task)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    print(f"[*] Basketball Coach Flask API running on http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
