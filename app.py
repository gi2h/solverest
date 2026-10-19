# app.py
import asyncio
import os
import time
import uuid
from typing import Dict, List, Optional, Union

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from turnstile_solver1 import solve_turnstile

# ---------------------------------------------------------------
# Config (env-overridable)
# ---------------------------------------------------------------
API_KEY          = os.environ.get("SOLVER_API_KEY", "").strip()
TASK_TTL_SECONDS = int(os.environ.get("TASK_TTL_SECONDS", "600"))
DEFAULT_TIMEOUT  = int(os.environ.get("SOLVER_TIMEOUT", "90"))

app = FastAPI(title="Fortress Captcha", version="4.1")
TASKS: Dict[str, dict] = {}


# ---------------------------------------------------------------
# Schema
# ---------------------------------------------------------------
class SolveRequest(BaseModel):
    sitekey: str = ""
    pageurl: str = Field(..., description="URL halaman target")
    type: Optional[str] = None           # "turnstile" — diterima, diabaikan
    proxy: Optional[str] = None
    cookies: Optional[Union[str, List[dict]]] = None
    user_agent: Optional[str] = None
    timeout: int = DEFAULT_TIMEOUT
    headless: Optional[bool] = None


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------
def _check_api_key(x_api_key: Optional[str]):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")


async def _cleanup_loop():
    while True:
        await asyncio.sleep(60)
        cutoff = time.time() - TASK_TTL_SECONDS
        for tid in [k for k, v in TASKS.items() if v["created_at"] < cutoff]:
            TASKS.pop(tid, None)


@app.on_event("startup")
async def _startup():
    asyncio.create_task(_cleanup_loop())


async def _run_task(task_id: str, req: SolveRequest):
    task = TASKS.get(task_id)
    if task is None:
        return
    task["status"] = "running"
    task["started_at"] = time.time()
    try:
        result = await solve_turnstile(
            sitekey=req.sitekey,
            pageurl=req.pageurl,
            proxy=req.proxy,
            cookies=req.cookies,
            user_agent=req.user_agent,
            timeout=req.timeout,
            headless=req.headless,
        )
        task["attempts"]   = result.get("attempts")
        task["elapsed_ms"] = result.get("elapsed_ms")
        if result.get("success"):
            task["status"] = "done"
            task["result"] = result.get("token")      # <-- string token
            task["error"]  = None
        else:
            task["status"] = "failed"
            task["result"] = None
            task["error"]  = result.get("error", "unknown_error")  # <-- string
    except Exception as e:
        task["status"] = "failed"
        task["result"] = None
        task["error"]  = f"{type(e).__name__}: {e}"
    finally:
        task["finished_at"] = time.time()


def _submit(req: SolveRequest) -> dict:
    task_id = str(uuid.uuid4())
    TASKS[task_id] = {
        "id": task_id,
        "status": "pending",
        "created_at": time.time(),
        "started_at": None,
        "finished_at": None,
        "result": None,
        "error": None,
        "attempts": None,
        "elapsed_ms": None,
    }
    asyncio.create_task(_run_task(task_id, req))
    # Kembalikan ketiga alias id supaya bot versi apa pun tetap cocok
    return {
        "id": task_id,
        "task_id": task_id,
        "taskId": task_id,
        "status": "pending",
    }


# ---------------------------------------------------------------
# Routes
# ---------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "version": app.version, "tasks_in_memory": len(TASKS)}


@app.get("/")
async def root():
    return {
        "name": "Fortress Captcha",
        "version": app.version,
        "endpoints": [
            "POST /v1/solve/turnstile   (dipakai sig25.py)",
            "POST /v1/tasks             (alias)",
            "GET  /v1/tasks/{id}",
            "GET  /v1/tasks/{id}/wait?timeout=5",
            "GET  /health",
        ],
    }


@app.post("/v1/solve/turnstile")
async def solve_endpoint(req: SolveRequest, x_api_key: Optional[str] = Header(None)):
    _check_api_key(x_api_key)
    return _submit(req)


@app.post("/v1/tasks")
async def create_task(req: SolveRequest, x_api_key: Optional[str] = Header(None)):
    _check_api_key(x_api_key)
    return _submit(req)


@app.get("/v1/tasks/{task_id}")
async def get_task(task_id: str):
    task = TASKS.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return task


@app.get("/v1/tasks/{task_id}/wait")
async def wait_task(task_id: str, timeout: int = 5):
    task = TASKS.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    timeout = max(1, min(timeout, 60))
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if task["status"] in ("done", "failed"):
            return task
        await asyncio.sleep(0.2)
    return task


# ---------------------------------------------------------------
# Dev runner
# ---------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
