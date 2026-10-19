# app.py
import asyncio
import os
import time
import uuid
from typing import Dict, List, Optional, Union

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from turnstile_solver1 import solve_turnstile

app = FastAPI(title="Fortress Captcha", version="4.1")

TASKS: Dict[str, dict] = {}
TASK_TTL_SECONDS = 600  # bersihkan task lama > 10 menit


# ---------------------------------------------------------------
# Schema
# ---------------------------------------------------------------

class SolveRequest(BaseModel):
    sitekey: str = Field(default="", description="Turnstile sitekey")
    pageurl: str = Field(..., description="URL halaman target")
    proxy: Optional[str] = None
    cookies: Optional[Union[str, List[dict]]] = None
    user_agent: Optional[str] = None
    timeout: int = 90
    headless: Optional[bool] = None


class TaskCreateResponse(BaseModel):
    id: str
    status: str


# ---------------------------------------------------------------
# Background runner
# ---------------------------------------------------------------

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
        task["status"] = "done" if result.get("success") else "failed"
        task["result"] = result
    except Exception as e:
        task["status"] = "failed"
        task["result"] = {
            "success": False,
            "error": f"{type(e).__name__}: {e}",
        }
    finally:
        task["finished_at"] = time.time()


# ---------------------------------------------------------------
# Routes
# ---------------------------------------------------------------

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": app.version,
        "tasks_in_memory": len(TASKS),
    }


@app.get("/")
async def root():
    return {
        "name": "Fortress Captcha",
        "version": app.version,
        "endpoints": [
            "POST /v1/tasks",
            "GET  /v1/tasks/{id}",
            "GET  /v1/tasks/{id}/wait?timeout=5",
            "GET  /health",
        ],
    }


@app.post("/v1/tasks", response_model=TaskCreateResponse)
async def create_task(req: SolveRequest):
    task_id = str(uuid.uuid4())
    TASKS[task_id] = {
        "id": task_id,
        "status": "pending",
        "created_at": time.time(),
        "result": None,
        "request": {
            "pageurl": req.pageurl,
            "sitekey": req.sitekey,
            "proxy": bool(req.proxy),
        },
    }
    asyncio.create_task(_run_task(task_id, req))
    return {"id": task_id, "status": "pending"}


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

    # clamp biar tidak menggantung terlalu lama
    timeout = max(1, min(timeout, 60))
    deadline = asyncio.get_event_loop().time() + timeout

    while asyncio.get_event_loop().time() < deadline:
        if task["status"] in ("done", "failed"):
            return task
        await asyncio.sleep(0.2)

    return task  # masih pending, client polling lagi


# ---------------------------------------------------------------
# Dev runner (production pakai `uvicorn app:app` via start.sh)
# ---------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
