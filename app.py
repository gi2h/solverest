#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import asyncio
import os
import uuid
import time
from typing import Optional, Dict
from fastapi import FastAPI, HTTPException, Request, BackgroundTasks
from pydantic import BaseModel
import uvicorn

from turnstile_solver1 import solve_turnstile, solve_recaptcha, get_cloudflare_cookie

app = FastAPI(title="Fortress Captcha Solver")

# Task Store.
class TaskResult(BaseModel):
    id: str
    status: str          # pending | done | failed
    result: Optional[str] = None
    error: Optional[str] = None
    created_at: float
    updated_at: float
    proxy_used: Optional[str] = None

TASKS: Dict[str, TaskResult] = {}
TASKS_LOCK = asyncio.Lock()

async def _task_create(proxy_used: Optional[str]) -> str:
    tid = str(uuid.uuid4())
    async with TASKS_LOCK:
        TASKS[tid] = TaskResult(
            id=tid, status="pending", created_at=time.time(),
            updated_at=time.time(), proxy_used=proxy_used,
        )
    return tid

async def _task_update(tid: str, status: str, result: Optional[str] = None, error: Optional[str] = None) -> None:
    async with TASKS_LOCK:
        t = TASKS.get(tid)
        if t:
            t.status = status
            t.result = result
            t.error = error
            t.updated_at = time.time()

class SolveReq(BaseModel):
    sitekey: str
    pageurl: str
    proxy: Optional[str] = None
    kind: str = "turnstile"

async def _run_task(tid: str, sitekey: str, pageurl: str, proxy: Optional[str], kind: str) -> None:
    try:
        if kind == "recaptcha":
            tok = await solve_recaptcha(sitekey, pageurl, proxy)
        else:
            tok = await solve_turnstile(sitekey, pageurl, proxy)

        if tok:
            await _task_update(tid, "done", result=tok)
        else:
            await _task_update(tid, "failed", error=f"navigation_failed or timeout on {pageurl}")
    except Exception as e:
        await _task_update(tid, "failed", error=str(e)[:200])

@app.get("/health")
async def health():
    return {"ok": True, "version": "4.1.0", "solver": "patchright", "tasks": len(TASKS)}

@app.post("/v1/solve/turnstile")
async def submit_turnstile(req: SolveReq, bg: BackgroundTasks):
    tid = await _task_create(req.proxy)
    bg.add_task(_run_task, tid, req.sitekey, req.pageurl, req.proxy, req.kind)
    return {"id": tid}

@app.get("/v1/tasks/{tid}/wait")
async def task_wait(tid: str, timeout: int = 60):
    deadline = time.time() + max(1, min(timeout, 180))
    while time.time() < deadline:
        async with TASKS_LOCK:
            t = TASKS.get(tid)
        if not t:
            raise HTTPException(status_code=404, detail="unknown task")
        if t.status in ("done", "failed"):
            return {"status": t.status, "result": t.result, "error": t.error}
        await asyncio.sleep(0.5)
    return {"status": "pending", "result": None, "error": None}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
