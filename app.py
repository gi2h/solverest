"""
Fortress Captcha API v4.2.2 - Patchright + Xvfb solver.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import struct
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Literal, Optional

import httpx
from fastapi import BackgroundTasks, Body, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from turnstile_solver1 import (
    get_cloudflare_cookie,
    solve_recaptcha,
    solve_turnstile,
)

logging.basicConfig(
    level=logging.INFO,
    format="[app] %(asctime)s %(levelname)s %(message)s",
)

# =====================================================================
# CONFIG
# =====================================================================
VISION_URL = os.getenv("VISION_URL", "http://vision-solver.railway.internal:9002")
TASK_TTL = int(os.getenv("TASK_TTL_SECONDS", "3600"))
DONE_TASK_TTL = int(os.getenv("DONE_TASK_TTL_SECONDS", "600"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_SOLVES", "2"))
MAX_QUEUE_SIZE = int(os.getenv("MAX_QUEUE_SIZE", "6"))
SOLVE_TIMEOUT = int(os.getenv("SOLVE_TIMEOUT", "180"))
BROWSER_TIMEOUT = int(os.getenv("BROWSER_TIMEOUT", "90"))
API_KEY = os.getenv("API_KEY", "")

QUEUE_FULL_MSG = "slot habis, ulangi lagi nanti"

# =====================================================================
# MODELS
# =====================================================================
class AltchaChallenge(BaseModel):
    algorithm: str = "SHA-256"
    challenge: str
    salt: str
    maxnumber: int = 1_000_000

class AltchaRequest(BaseModel):
    challenge: AltchaChallenge

class AltchaResponse(BaseModel):
    solution: str
    iterations: int
    elapsed_ms: int

class FriendlyRequest(BaseModel):
    challenge: str
    difficulty: int = 16
    max_nonce: int = 10_000_000

class FriendlyResponse(BaseModel):
    solution: str
    nonce: int
    elapsed_ms: int

class ImageRequest(BaseModel):
    image_base64: str
    hint: Optional[str] = None

class BrowserRequest(BaseModel):
    sitekey: str
    pageurl: str
    action: Optional[str] = None
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

# =====================================================================
# STORES
# =====================================================================
_legacy_tasks: dict[str, dict[str, Any]] = {}
_legacy_lock = asyncio.Lock()

class TaskStore:
    def __init__(self):
        self._tasks: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._cleanup: Optional[asyncio.Task] = None

    async def start(self):
        self._cleanup = asyncio.create_task(self._cleanup_loop())

    async def stop(self):
        if self._cleanup:
            self._cleanup.cancel()
            try:
                await self._cleanup
            except asyncio.CancelledError:
                pass

    async def create_if_slot(self, task_type: str) -> Optional[str]:
        async with self._lock:
            pending = sum(1 for t in self._tasks.values() if t["status"] == "pending")
            if pending >= MAX_QUEUE_SIZE:
                return None
            tid = str(uuid.uuid4())
            self._tasks[tid] = {
                "id": tid, "type": task_type, "status": "pending",
                "result": None, "cookies": None, "user_agent": None, 
                "error": None, "elapsed_ms": None,
                "created_at": time.time(), "updated_at": time.time(),
            }
            return tid

    async def get(self, tid: str) -> Optional[dict]:
        async with self._lock:
            return dict(self._tasks[tid]) if tid in self._tasks else None

    async def update(self, tid: str, **kw):
        async with self._lock:
            if tid in self._tasks:
                self._tasks[tid].update(kw)
                self._tasks[tid]["updated_at"] = time.time()

    async def delete(self, tid: str) -> bool:
        async with self._lock:
            return self._tasks.pop(tid, None) is not None

    async def _cleanup_loop(self):
        while True:
            await asyncio.sleep(30)
            now = time.time()
            pending_cutoff = now - TASK_TTL
            done_cutoff = now - DONE_TASK_TTL

            async with self._lock:
                to_delete = []
                for k, v in self._tasks.items():
                    if v["status"] in ("done", "failed"):
                        if v["updated_at"] < done_cutoff: to_delete.append(k)
                    else:
                        if v["created_at"] < pending_cutoff: to_delete.append(k)
                for k in to_delete:
                    self._tasks.pop(k, None)

            if _legacy_tasks:
                async with _legacy_lock:
                    legacy_delete = []
                    for k, v in _legacy_tasks.items():
                        created = v.get("created_at", 0)
                        if v.get("status") in ("done", "failed"):
                            if created < done_cutoff: legacy_delete.append(k)
                        else:
                            if created < pending_cutoff: legacy_delete.append(k)
                    for k in legacy_delete:
                        _legacy_tasks.pop(k, None)

store = TaskStore()
semaphore = asyncio.Semaphore(MAX_CONCURRENT)

async def get_any_task(tid: str) -> Optional[dict]:
    t = await store.get(tid)
    if t: return t
    async with _legacy_lock:
        if tid in _legacy_tasks:
            lt = _legacy_tasks[tid]
            return {
                "id": lt.get("id", tid), "type": lt.get("type", "unknown"),
                "status": lt.get("status", "pending"), "result": lt.get("result"),
                "cookies": None, "user_agent": None, "error": lt.get("error"),
                "elapsed_ms": None, "created_at": lt.get("created_at", time.time()),
                "updated_at": time.time(),
            }
    return None

# =====================================================================
# POW SOLVERS
# =====================================================================
def _solve_altcha(ch: AltchaChallenge) -> tuple[str, int]:
    max_n = min(ch.maxnumber, 5_000_000)
    for n in range(max_n):
        if hashlib.sha256(f"{ch.salt}{n}".encode()).hexdigest() == ch.challenge:
            solution = {"algorithm": ch.algorithm, "challenge": ch.challenge, "number": n, "salt": ch.salt}
            return base64.b64encode(json.dumps(solution).encode()).decode(), n
    raise ValueError(f"no_solution_found_within_{max_n}_iterations")

def _solve_friendly(challenge_b64: str, difficulty: int, max_nonce: int) -> tuple[str, int]:
    buf = base64.b64decode(challenge_b64)
    threshold = 1 << (32 - difficulty)
    for nonce in range(max_nonce):
        b = buf + struct.pack(">Q", nonce)
        h = hashlib.blake2b(b, digest_size=32).digest()
        if struct.unpack("<I", h[:4])[0] < threshold:
            return base64.b64encode(b).decode(), nonce
    raise ValueError(f"no_solution_found_within_{max_nonce}_nonces")

# =====================================================================
# IMAGE SOLVER
# =====================================================================
async def _solve_image_via_vision(image_b64: str, hint: Optional[str]) -> str:
    async with httpx.AsyncClient(timeout=SOLVE_TIMEOUT) as c:
        try:
            r = await c.post(f"{VISION_URL}/solve-text", json={"image": image_b64, "hint": hint or ""})
        except httpx.ConnectError as e:
            raise RuntimeError(f"vision_unreachable: {e}")
        except httpx.TimeoutException:
            raise RuntimeError(f"vision_timeout_after_{SOLVE_TIMEOUT}s")

        if r.status_code != 200:
            raise RuntimeError(f"vision_http_{r.status_code}: {r.text[:200]}")
        data = r.json()
        text = (data.get("text") or data.get("result") or "").strip()
        if not text: raise RuntimeError("vision_returned_empty")
        return text

# =====================================================================
# BROWSER SOLVER WRAPPER
# =====================================================================
def _run_async_in_thread(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

async def _run_browser_task(tid: str, sitekey: str, pageurl: str, kind: Literal["turnstile", "recaptcha"], proxy: Optional[str] = None):
    async with semaphore:
        await store.update(tid, status="processing")
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

            await store.update(tid, status="done", result=token, cookies=cookies, user_agent=ua, elapsed_ms=int((time.time() - t0) * 1000))
        except Exception as e:
            await store.update(tid, status="failed", error=str(e)[:500], elapsed_ms=int((time.time() - t0) * 1000))

async def _run_image_task(tid: str, image_b64: str, hint: Optional[str]):
    async with semaphore:
        await store.update(tid, status="processing")
        t0 = time.time()
        try:
            result = await _solve_image_via_vision(image_b64, hint)
            await store.update(tid, status="done", result=result, elapsed_ms=int((time.time() - t0) * 1000))
        except Exception as e:
            await store.update(tid, status="failed", error=str(e)[:500], elapsed_ms=int((time.time() - t0) * 1000))

# =====================================================================
# LIFESPAN & APP
# =====================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await store.start()
    print(f"[app] started | version=4.2.2 | solver=patchright+xvfb | max_queue={MAX_QUEUE_SIZE}")
    yield
    await store.stop()

app = FastAPI(title="Fortress Captcha API", version="4.2.2", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

def require_api_key(request: Request):
    if not API_KEY: return
    provided = request.headers.get("x-api-key", "") or request.headers.get("key", "")
    if provided != API_KEY: raise HTTPException(401, "invalid_api_key")

@app.get("/health")
async def health():
    async with store._lock:
        pending = sum(1 for t in store._tasks.values() if t["status"] == "pending")
        processing = sum(1 for t in store._tasks.values() if t["status"] == "processing")
        done = sum(1 for t in store._tasks.values() if t["status"] == "done")
        failed = sum(1 for t in store._tasks.values() if t["status"] == "failed")
        total = len(store._tasks)
    return {"status": "ok", "tasks": {"total": total, "pending": pending, "processing": processing, "done": done, "failed": failed}, "queue": {"size": pending, "max": MAX_QUEUE_SIZE}}

# =====================================================================
# ENDPOINTS
# =====================================================================
@app.post("/v1/solve/image", response_model=TaskResponse, status_code=202)
async def submit_image(req: ImageRequest, bg: BackgroundTasks, _=Depends(require_api_key)):
    tid = await store.create_if_slot("image")
    if not tid: raise HTTPException(429, QUEUE_FULL_MSG)
    bg.add_task(_run_image_task, tid, req.image_base64, req.hint)
    return TaskResponse(**await store.get(tid))

@app.post("/v1/solve/turnstile", response_model=TaskResponse, status_code=202)
async def submit_turnstile(req: BrowserRequest, bg: BackgroundTasks, _=Depends(require_api_key)):
    if not req.sitekey or not req.pageurl: raise HTTPException(400, "sitekey_and_pageurl_required")
    tid = await store.create_if_slot("turnstile")
    if not tid: raise HTTPException(429, QUEUE_FULL_MSG)
    bg.add_task(_run_browser_task, tid, req.sitekey, req.pageurl, "turnstile", req.proxy)
    return TaskResponse(**await store.get(tid))

@app.post("/v1/solve/recaptcha", response_model=TaskResponse, status_code=202)
async def submit_recaptcha(req: BrowserRequest, bg: BackgroundTasks, _=Depends(require_api_key)):
    if not req.sitekey or not req.pageurl: raise HTTPException(400, "sitekey_and_pageurl_required")
    tid = await store.create_if_slot("recaptcha")
    if not tid: raise HTTPException(429, QUEUE_FULL_MSG)
    bg.add_task(_run_browser_task, tid, req.sitekey, req.pageurl, "recaptcha", req.proxy)
    return TaskResponse(**await store.get(tid))

@app.get("/v1/tasks/{tid}", response_model=TaskResponse)
async def get_task(tid: str, _=Depends(require_api_key)):
    t = await get_any_task(tid)
    if not t: raise HTTPException(404, "task_not_found")
    return TaskResponse(**t)

@app.get("/v1/tasks/{tid}/wait", response_model=TaskResponse)
async def wait_task(tid: str, timeout: int = 60, _=Depends(require_api_key)):
    if timeout > 300: raise HTTPException(400, "timeout_max_300")
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = await get_any_task(tid)
        if not t:
            return TaskResponse(id=tid, type="unknown", status="failed", error="task_lost_or_not_found", created_at=time.time(), updated_at=time.time())
        if t["status"] in ("done", "failed"):
            return TaskResponse(**t)
        await asyncio.sleep(0.5)
    t = await get_any_task(tid)
    if not t: return TaskResponse(id=tid, type="unknown", status="failed", error="timeout", created_at=time.time(), updated_at=time.time())
    return TaskResponse(**t)

@app.delete("/v1/tasks/{tid}")
async def delete_task(tid: str, _=Depends(require_api_key)):
    if not await store.delete(tid): raise HTTPException(404, "task_not_found")
    return {"deleted": tid}

# =====================================================================
# LEGACY ENDPOINTS (Sederhana)
# =====================================================================
def _legacy_response(tid: str, task: dict) -> dict:
    status = task.get("status", "pending")
    if status == "done": return {"taskId": tid, "status": "done", "token": task.get("result", ""), "solution": {"token": task.get("result", "")}}
    if status == "failed": return {"taskId": tid, "status": "error", "error": task.get("error", "failed")}
    return {"taskId": tid, "status": status}

@app.post("/solve")
async def legacy_solve(body: dict = Body(...)):
    ttype = (body.get("type") or "").lower()
    
    if "taskId" in body:
        t = await get_any_task(str(body["taskId"]))
        if not t: return {"status": "error", "error": "task_not_found"}
        return _legacy_response(str(body["taskId"]), t)

    if ttype == "turnstile":
        sitekey = body.get("siteKey") or body.get("sitekey") or ""
        pageurl = body.get("domain") or body.get("url") or ""
        if not sitekey or not pageurl: return {"taskId": None, "status": "error", "error": "missing_params"}
        tid = await store.create_if_slot("turnstile")
        if not tid: return {"taskId": None, "status": "error", "error": "queue_full", "message": QUEUE_FULL_MSG}
        asyncio.create_task(_run_browser_task(tid, sitekey, pageurl, "turnstile", body.get("proxy")))
        return {"taskId": tid, "status": "pending"}

    return {"taskId": None, "status": "error", "error": "unknown_type"}
