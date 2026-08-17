import os
from typing import Optional

from flask import Flask, request, jsonify, send_file, Response

from flask_cors import CORS

import auth
import metrics
import plan_service
from pdf_export import generate_plan_pdf
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
PDF_RATE_LIMIT = (20, 60)        # 20 PDF exports per minute per IP


def _client_ip() -> str:
    """
    Client IP for quotas/limits. X-Forwarded-For is honored only behind an
    explicitly trusted reverse proxy (TRUSTED_PROXY=1) to prevent spoofing.
    """
    remote = request.remote_addr or "unknown"
    if os.environ.get("TRUSTED_PROXY") == "1":
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
    return remote


def _auth_rate_limited() -> Optional[tuple]:
    max_attempts, window = AUTH_RATE_LIMIT
    if is_rate_limited(_client_ip(), "auth", max_attempts, window):
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
        metrics.log_event("register", subject=res["username"])
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
        metrics.log_event("login", subject=res["username"])
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
    if is_rate_limited(_client_ip(), "generate", max_attempts, window):
        return jsonify({"detail": "Превышен лимит генераций планов (максимум 30 в час). Попробуйте позже."}), 429

    username = get_token_user()
    data = request.get_json(silent=True) or {}

    # Validation (shared rules with the FastAPI app)
    error = plan_service.validate_plan_params(data)
    if error:
        return jsonify({"detail": error}), 400

    # Daily quota (protects the LLM API budget)
    quota_error = plan_service.check_daily_quota(username, _client_ip())
    if quota_error:
        return jsonify({"detail": quota_error}), 429

    created = plan_service.create_task(data, username, client_ip=_client_ip())
    return jsonify({
        "status": "processing",
        "task_id": created["task_id"],
        "poll_token": created["poll_token"],
    })


@app.route("/plan_status/<task_id>", methods=["GET"])
def get_plan_status(task_id: str):
    max_attempts, window = POLL_RATE_LIMIT
    if is_rate_limited(_client_ip(), "poll", max_attempts, window):
        return jsonify({"detail": "Слишком много запросов статуса. Попробуйте позже."}), 429

    username = get_token_user()
    poll_token = request.headers.get("X-Poll-Token") or request.args.get("poll_token")
    task = plan_service.get_task(task_id, username=username, poll_token=poll_token)
    if not task:
        return jsonify({"status": "error", "message": "Task not found"})
    return jsonify(task)


@app.route("/api/pdf", methods=["POST"])
def export_pdf():
    """Renders a generated plan as a branded PDF (client sends the plan JSON)."""
    max_attempts, window = PDF_RATE_LIMIT
    if is_rate_limited(_client_ip(), "pdf", max_attempts, window):
        return jsonify({"detail": "Слишком много запросов PDF. Попробуйте позже."}), 429

    data = request.get_json(silent=True) or {}
    payload = data.get("payload") or {}
    api_result = data.get("apiResult") or {}
    if not api_result.get("data"):
        return jsonify({"detail": "Нет данных плана для экспорта."}), 400

    try:
        pdf_bytes = generate_plan_pdf(payload, api_result)
    except Exception as e:
        print(f"[PDF] Export failed: {type(e).__name__}: {e}", flush=True)
        return jsonify({"detail": "Не удалось создать PDF."}), 500
    metrics.log_event("pdf_exported", subject=get_token_user())

    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": "attachment; filename=training_plan.pdf"},
    )


@app.route("/api/admin/stats", methods=["GET"])
def admin_stats():
    """Product metrics endpoint, guarded by ADMIN_TOKEN (404 unless configured)."""
    admin_token = os.environ.get("ADMIN_TOKEN")
    if not admin_token:
        return jsonify({"detail": "Not found"}), 404
    provided = request.headers.get("X-Admin-Token", "")
    import hmac as _hmac
    if not _hmac.compare_digest(admin_token, provided):
        return jsonify({"detail": "Not found"}), 404
    return jsonify(metrics.get_stats())


@app.route("/landing", methods=["GET"])
def landing():
    return send_file("landing.html")


def _setup_logging() -> None:
    """Rotating app log next to the code; disable via COACH_LOG_FILE=0."""
    import logging
    from logging.handlers import RotatingFileHandler

    if os.environ.get("COACH_LOG_FILE") == "0":
        return
    try:
        handler = RotatingFileHandler("app.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(handler)
    except Exception as e:
        print(f"[LOG] File logging unavailable: {e}", flush=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    _setup_logging()

    if os.environ.get("FLASK_DEV") == "1":
        print(f"[*] Basketball Coach API (Flask dev server) on http://127.0.0.1:{port}")
        app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
    else:
        # Production-grade WSGI server (dev server is not suited for production)
        from waitress import serve

        print(f"[*] Basketball Coach API (waitress) on http://0.0.0.0:{port}")
        serve(app, host="0.0.0.0", port=port, threads=8)
