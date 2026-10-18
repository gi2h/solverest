"""
Fortress Captcha API v4.2.3 - Patchright + Xvfb solver.
"""
from __future__ import annotations
import asyncio, base64, hashlib, json, logging, os, struct, time, uuid
from contextlib import asynccontextmanager
from typing import Any, Literal, Optional
import httpx
from fastapi import BackgroundTasks, Body, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from turnstile_solver1 import solve_recaptcha, solve_turnstile

logging.basicConfig(level=logging.INFO, format="[app] %(asctime)s %(levelname)s %(message)s")

BROWSER_TIMEOUT = int(os.getenv("BROWSER_TIMEOUT", "90"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_SOLVES", "2"))
MAX_QUEUE_SIZE = int(os.getenv("MAX_QUEUE_SIZE", "6"))
API_KEY = os.getenv("API_KEY", "")
QUEUE_FULL_MSG = "slot habis, ulangi lagi nanti"

class BrowserRequest(BaseModel):
    sitekey: str
    pageurl: str
    proxy: Optional[str] = None

class TaskResponse(BaseModel):
    id: str
    type: str
    status: Literal["pending", "processing", "done", "failed"]
    result: Optional[str] = None
    cookies: Optional[dict] = None
    user_agent: Optional[str] = None
    error: Optional[str] = None
    elapsed_ms: Optional[int] = None
    created_at: float
    updated_at: float

store: dict[str, dict] = {}
semaphore = asyncio.Semaphore(MAX_CONCURRENT)

def _run_async_in_thread(coro):
    loop = asyncio.new_event_loop()
    try: return loop.run_until_complete(coro)
    finally: loop.close()

async def _run_browser_task(tid: str, sitekey: str, pageurl: str, kind: str, proxy: Optional[str] = None):
    async with semaphore:
        store[tid].update(status="processing", updated_at=time.time())
        t0 = time.time()
        try:
            if kind == "turnstile":
                coro = solve_turnstile(sitekey=sitekey, pageurl=pageurl, timeout=BROWSER_TIMEOUT, proxy=proxy, headless=False)
            else:
                coro = solve_recaptcha(sitekey=sitekey, pageurl=pageurl, timeout=BROWSER_TIMEOUT, proxy=proxy, headless=False)

            result = await asyncio.get_event_loop().run_in_executor(None, _run_async_in_thread, coro)
            
            token = result.get("token", "") if isinstance(result, dict) else str(result)
            cookies = result.get("cookies") if isinstance(result, dict) else None
            ua = result.get("user_agent") if isinstance(result, dict) else None

            if not token: raise RuntimeError("solver_returned_empty_token")

            store[tid].update(status="done", result=token, cookies=cookies, user_agent=ua, elapsed_ms=int((time.time() - t0) * 1000), updated_at=time.time())
        except Exception as e:
            store[tid].update(status="failed", error=str(e)[:500], elapsed_ms=int((time.time() - t0) * 1000), updated_at=time.time())

@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"[app] started | max_queue={MAX_QUEUE_SIZE}")
    yield

app = FastAPI(lifespan=lifespan)

def require_api_key(request: Request):
    if not API_KEY: return
    if request.headers.get("x-api-key") != API_KEY: raise HTTPException(401, "invalid_api_key")

@app.post("/v1/solve/turnstile", response_model=TaskResponse, status_code=202)
async def submit_turnstile(req: BrowserRequest, bg: BackgroundTasks, _=Depends(require_api_key)):
    if not req.sitekey or not req.pageurl: raise HTTPException(400, "missing_params")
    pending = sum(1 for t in store.values() if t["status"] == "pending")
    if pending >= MAX_QUEUE_SIZE: raise HTTPException(429, QUEUE_FULL_MSG)
    
    tid = str(uuid.uuid4())
    store[tid] = {"id": tid, "type": "turnstile", "status": "pending", "result": None, "cookies": None, "user_agent": None, "error": None, "created_at": time.time(), "updated_at": time.time()}
    bg.add_task(_run_browser_task, tid, req.sitekey, req.pageurl, "turnstile", req.proxy)
    return TaskResponse(**store[tid])

@app.get("/v1/tasks/{tid}/wait", response_model=TaskResponse)
async def wait_task(tid: str, timeout: int = 60, _=Depends(require_api_key)):
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = store.get(tid)
        if not t: return TaskResponse(id=tid, type="unknown", status="failed", error="task_not_found", created_at=time.time(), updated_at=time.time())
        if t["status"] in ("done", "failed"): return TaskResponse(**t)
        await asyncio.sleep(0.5)
    return TaskResponse(**store.get(tid, {"id": tid, "type": "unknown", "status": "failed", "error": "timeout", "created_at": time.time(), "updated_at": time.time()}))
