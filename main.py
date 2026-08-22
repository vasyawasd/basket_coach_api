import hmac
import os
from typing import Optional, List, Union

from fastapi import FastAPI, Header, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

import auth
import metrics
import plan_service
from pdf_export import generate_plan_pdf
from rate_limiter import is_rate_limited

app = FastAPI(title="Basketball Coach API")

app.add_middleware(
    CORSMiddleware,
    # Restrict CORS to local development
    allow_origins=["http://localhost:8000", "http://127.0.0.1:8000"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-Poll-Token"],
)


@app.middleware("http")
async def set_security_headers(request: Request, call_next):
    """Injects standard HTTP security headers to all responses."""
    response = await call_next(request)
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
        "connect-src 'self' http://localhost:8000 http://127.0.0.1:8000;"
    )
    return response


# Rate limit policies (sliding window, backed by SQLite: survives restarts, multi-worker safe)
AUTH_RATE_LIMIT = (10, 60)       # 10 auth attempts per minute per IP
GEN_RATE_LIMIT = (30, 3600)      # 30 plan generations per hour per IP
POLL_RATE_LIMIT = (120, 60)      # 120 status polls per minute per IP (frontend polls every 3s)
PDF_RATE_LIMIT = (20, 60)        # 20 PDF exports per minute per IP


def _client_ip(request: Request) -> str:
    """
    Client IP for quotas/limits. X-Forwarded-For is honored only behind an
    explicitly trusted reverse proxy (TRUSTED_PROXY=1) to prevent spoofing.
    """
    remote = request.client.host if request.client else "unknown"
    if os.environ.get("TRUSTED_PROXY") == "1":
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
    return remote


def _rate_limit_dependency(kind: str, policy: tuple):
    max_attempts, window = policy

    def check(request: Request):
        ip = request.client.host if request.client else "unknown"
        if is_rate_limited(ip, kind, max_attempts, window):
            raise HTTPException(status_code=429, detail="Слишком много запросов. Попробуйте позже.")

    return check


class PlayerParams(BaseModel):
    height: int = Field(..., gt=50, lt=300, description="Height in cm")
    weight: float = Field(..., gt=30, lt=300, description="Weight in kg")
    position: str = Field(..., max_length=50, description="Player position")
    goal: Union[str, List[str]] = Field(..., description="Training goal or list of goals")
    days_per_week: int = Field(..., ge=1, le=7, description="Training days per week")
    injuries: Optional[str] = Field(None, max_length=500, description="Existing injuries or limitations")
    model: Optional[str] = Field("qwen3.8-max-preview", max_length=50, description="Selected AI Model")
    feedback: Optional[str] = Field(None, max_length=600, description="Athlete feedback for plan adaptation")


class AuthSchema(BaseModel):
    username: str = Field(..., min_length=3, max_length=30)
    password: str = Field(..., min_length=6, max_length=100)


def get_token_user(authorization: Optional[str] = Header(None)) -> Optional[str]:
    """Helper dependency to extract username from Bearer authorization token."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    return auth.get_current_user(token)


@app.get("/")
async def root():
    return FileResponse("index.html")


# Auth Endpoints
auth_rate_limit = _rate_limit_dependency("auth", AUTH_RATE_LIMIT)
gen_rate_limit = _rate_limit_dependency("generate", GEN_RATE_LIMIT)
poll_rate_limit = _rate_limit_dependency("poll", POLL_RATE_LIMIT)


@app.post("/api/register")
async def register(data: AuthSchema, _=Depends(auth_rate_limit)):
    try:
        res = auth.register_user(data.username, data.password)
        metrics.log_event("register", subject=res["username"])
        return {"status": "success", "username": res["username"], "token": res["token"]}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/login")
async def login(data: AuthSchema, _=Depends(auth_rate_limit)):
    try:
        res = auth.login_user(data.username, data.password)
        metrics.log_event("login", subject=res["username"])
        return {"status": "success", "username": res["username"], "token": res["token"]}
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.get("/api/me")
async def get_me(username: Optional[str] = Depends(get_token_user)):
    if not username:
        return {"authenticated": False}
    return {"authenticated": True, "username": username}


@app.post("/api/logout")
async def logout(authorization: Optional[str] = Header(None)):
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ", 1)[1].strip()
        auth.logout_user(token)
    # Idempotent: always success, even with an invalid or missing token
    return {"status": "success"}


@app.get("/api/history")
async def user_history(username: Optional[str] = Depends(get_token_user)):
    if not username:
        raise HTTPException(status_code=401, detail="Требуется авторизация.")
    return {"status": "success", "history": auth.get_user_history(username)}


@app.delete("/api/history/{item_id}")
async def delete_history_item(item_id: str, username: Optional[str] = Depends(get_token_user)):
    if not username:
        raise HTTPException(status_code=401, detail="Требуется авторизация.")
    success = auth.delete_user_history_item(username, item_id)
    return {"status": "success" if success else "error"}


@app.delete("/api/history")
async def clear_history(username: Optional[str] = Depends(get_token_user)):
    if not username:
        raise HTTPException(status_code=401, detail="Требуется авторизация.")
    auth.clear_user_history(username)
    return {"status": "success"}


@app.post("/generate_plan")
async def generate_plan(
    params: PlayerParams,
    request: Request,
    username: Optional[str] = Depends(get_token_user),
    _=Depends(gen_rate_limit),
):
    # Extra shared rules beyond Pydantic bounds (field length caps)
    error = plan_service.validate_plan_params(params.dict())
    if error:
        raise HTTPException(status_code=400, detail=error)

    # Daily quota (protects the LLM API budget)
    quota_error = plan_service.check_daily_quota(username, _client_ip(request))
    if quota_error:
        raise HTTPException(status_code=429, detail=quota_error)

    created = plan_service.create_task(params.dict(), username, client_ip=_client_ip(request))
    return {
        "status": "processing",
        "task_id": created["task_id"],
        "poll_token": created["poll_token"],
    }


@app.get("/plan_status/{task_id}")
async def get_plan_status(
    task_id: str,
    username: Optional[str] = Depends(get_token_user),
    x_poll_token: Optional[str] = Header(None, alias="X-Poll-Token"),
    _=Depends(poll_rate_limit),
):
    task = plan_service.get_task(task_id, username=username, poll_token=x_poll_token)
    if not task:
        return {"status": "error", "message": "Task not found"}
    return task


class PdfExportSchema(BaseModel):
    payload: dict = {}
    apiResult: dict


pdf_rate_limit = _rate_limit_dependency("pdf", PDF_RATE_LIMIT)


@app.post("/api/pdf")
async def export_pdf(data: PdfExportSchema, _=Depends(pdf_rate_limit)):
    """Renders a generated plan as a branded PDF (client sends the plan JSON)."""
    if not data.apiResult.get("data"):
        raise HTTPException(status_code=400, detail="Нет данных плана для экспорта.")
    try:
        pdf_bytes = generate_plan_pdf(data.payload, data.apiResult)
    except Exception as e:
        print(f"[PDF] Export failed: {type(e).__name__}: {e}", flush=True)
        raise HTTPException(status_code=500, detail="Не удалось создать PDF.")
    metrics.log_event("pdf_exported")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=training_plan.pdf"},
    )


@app.get("/api/admin/stats")
async def admin_stats(x_admin_token: Optional[str] = Header(None)):
    """Product metrics endpoint, guarded by ADMIN_TOKEN (404 unless configured)."""
    admin_token = os.environ.get("ADMIN_TOKEN")
    if not admin_token or not hmac.compare_digest(admin_token, x_admin_token or ""):
        raise HTTPException(status_code=404, detail="Not found")
    return metrics.get_stats()


@app.get("/landing", response_class=FileResponse)
async def landing():
    return FileResponse("landing.html")

