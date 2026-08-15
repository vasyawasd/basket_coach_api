import uuid
import threading
import time
from typing import Optional, Dict, Any, List, Union
from fastapi import FastAPI, Header, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from rag import get_relevant_knowledge, sanitize_input
from llm_service import call_llm_api
import auth

app = FastAPI(title="Basketball Coach API")

app.add_middleware(
    CORSMiddleware,
    # ponytail: VULN-004 fix — restrict to local dev; set your domain in prod
    allow_origins=["http://localhost:8000", "http://127.0.0.1:8000"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

# In-memory task database for polling
tasks_db: Dict[str, Dict[str, Any]] = {}

# VULN-005 fix: simple in-memory rate limiter for auth endpoints
_rate_limit: Dict[str, list] = {}
RATE_LIMIT_WINDOW = 60   # seconds
RATE_LIMIT_MAX = 10      # max attempts per window per IP


async def rate_limit_check(request: Request):
    """Dependency that blocks IPs exceeding RATE_LIMIT_MAX auth requests per minute."""
    ip = request.client.host if request.client else "unknown"
    now = time.time()
    attempts = [t for t in _rate_limit.get(ip, []) if t > now - RATE_LIMIT_WINDOW]
    if len(attempts) >= RATE_LIMIT_MAX:
        raise HTTPException(status_code=429, detail="Слишком много запросов. Попробуйте позже.")
    attempts.append(now)
    _rate_limit[ip] = attempts


class PlayerParams(BaseModel):
    height: int = Field(..., gt=50, lt=300, description="Height in cm")
    weight: float = Field(..., gt=30, lt=300, description="Weight in kg")
    position: str = Field(..., max_length=50, description="Player position")
    goal: Union[str, List[str]] = Field(..., description="Training goal or list of goals")
    days_per_week: int = Field(..., ge=1, le=7, description="Training days per week")
    injuries: Optional[str] = Field(None, max_length=500, description="Existing injuries or limitations")
    model: Optional[str] = Field("qwen3.8-max-preview", max_length=50, description="Selected AI Model")


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
@app.post("/api/register")
async def register(data: AuthSchema, _=Depends(rate_limit_check)):
    try:
        res = auth.register_user(data.username, data.password)
        return {"status": "success", "username": res["username"], "token": res["token"]}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/login")
async def login(data: AuthSchema, _=Depends(rate_limit_check)):
    try:
        res = auth.login_user(data.username, data.password)
        return {"status": "success", "username": res["username"], "token": res["token"]}
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.get("/api/me")
async def get_me(username: Optional[str] = Depends(get_token_user)):
    if not username:
        return {"authenticated": False}
    return {"authenticated": True, "username": username}


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


def process_plan_task(task_id: str, params: PlayerParams, username: Optional[str] = None):
    """Background task to process RAG and LLM generation (runs in a separate thread)."""
    try:
        print(f"[TASK {task_id[:8]}] Starting plan generation with model '{params.model}'...")
        clean_position = sanitize_input(params.position)
        if isinstance(params.goal, list):
            clean_goal = ", ".join([sanitize_input(g) for g in params.goal])
        else:
            clean_goal = sanitize_input(params.goal)
        clean_injuries = sanitize_input(params.injuries or "None")

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
            f"- Height: {params.height} cm\n"
            f"- Weight: {params.weight} kg\n"
            f"- Position: {clean_position}\n"
            f"- Goal: {clean_goal}\n"
            f"- Days per week: {params.days_per_week}\n"
            f"- Injuries/Limitations: {clean_injuries}"
        )

        print(f"[TASK {task_id[:8]}] RAG context ready ({len(rag_context)} chars), calling LLM ({params.model})...")
        llm_result = call_llm_api(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            context_text=rag_context,
            selected_model=params.model,
        )
        print(f"[TASK {task_id[:8]}] LLM returned, source: {llm_result.get('source')}")

        result_payload = {
            "status": "success",
            "source": llm_result.get("source"),
            "rag_context_snippet": rag_context[:300] + ("..." if len(rag_context) > 300 else ""),
            "data": llm_result.get("data"),
        }

        tasks_db[task_id] = result_payload

        # If user is authenticated, automatically persist to backend user history
        if username:
            import datetime
            now_str = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
            history_item = {
                "id": f"plan_{int(datetime.datetime.now().timestamp() * 1000)}",
                "timestamp": now_str,
                "payload": params.dict(),
                "apiResult": result_payload
            }
            auth.add_user_history_item(username, history_item)

    except Exception as e:
        print(f"[TASK {task_id[:8]}] FAILED: {type(e).__name__}: {e}")
        tasks_db[task_id] = {
            "status": "error",
            "message": str(e)
        }


@app.post("/generate_plan")
async def generate_plan(params: PlayerParams, username: Optional[str] = Depends(get_token_user)):
    task_id = str(uuid.uuid4())
    tasks_db[task_id] = {"status": "processing", "created_at": time.time(), "owner": username}

    # VULN-003 fix: cleanup tasks older than 1 hour
    now = time.time()
    expired = [t for t, d in tasks_db.items() if d.get("created_at", 0) < now - 3600]
    for t in expired:
        del tasks_db[t]

    # ponytail: thread instead of BackgroundTasks — sync OpenAI client blocks event loop
    threading.Thread(target=process_plan_task, args=(task_id, params, username), daemon=True).start()
    return {"status": "processing", "task_id": task_id}


@app.get("/plan_status/{task_id}")
async def get_plan_status(task_id: str, username: Optional[str] = Depends(get_token_user)):
    task = tasks_db.get(task_id)
    if not task:
        return {"status": "error", "message": "Task not found"}
    # VULN-007 fix: if task has owner, only owner can view
    if task.get("owner") and task["owner"] != username:
        return {"status": "error", "message": "Task not found"}
    return task
