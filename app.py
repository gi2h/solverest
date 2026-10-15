"""
Fortress Captcha API v2 — Self-contained, modern REST.

No 2captcha compatibility. No Redis. No API key required by default.
Endpoints:
  POST /v1/solve/altcha     → sync solve
  POST /v1/solve/friendly   → sync solve
  POST /v1/solve/image      → async (returns task_id)
  GET  /v1/tasks/{id}       → task status
  GET  /v1/tasks/{id}/wait  → long-poll
  DELETE /v1/tasks/{id}     → delete
  GET  /health              → health
  GET  /                    → info
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import struct
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Literal, Optional

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# ═══════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════
VISION_URL = os.getenv("VISION_URL", "http://vision-solver.railway.internal:9002")
TASK_TTL = int(os.getenv("TASK_TTL_SECONDS", "3600"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_SOLVES", "8"))
SOLVE_TIMEOUT = int(os.getenv("SOLVE_TIMEOUT", "180"))
API_KEY = os.getenv("API_KEY", "")  # empty = public access

# ═══════════════════════════════════════════════════════════════════════
# MODELS
# ═══════════════════════════════════════════════════════════════════════
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
    challenge: str = Field(..., description="Base64 challenge buffer")
    difficulty: int = 16
    max_nonce: int = 10_000_000

class FriendlyResponse(BaseModel):
    solution: str
    nonce: int
    elapsed_ms: int

class ImageRequest(BaseModel):
    image_base64: str
    hint: Optional[str] = None

class TaskResponse(BaseModel):
    id: str
    type: str
    status: Literal["pending", "processing", "done", "failed"]
    result: Optional[str] = None
    error: Optional[str] = None
    elapsed_ms: Optional[int] = None
    created_at: float
    updated_at: float

# ═══════════════════════════════════════════════════════════════════════
# IN-MEMORY TASK STORE
# ═══════════════════════════════════════════════════════════════════════
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

    async def create(self, task_type: str) -> str:
        tid = str(uuid.uuid4())
        async with self._lock:
            self._tasks[tid] = {
                "id": tid, "type": task_type, "status": "pending",
                "result": None, "error": None, "elapsed_ms": None,
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
            await asyncio.sleep(60)
            cutoff = time.time() - TASK_TTL
            async with self._lock:
                for k in [k for k, v in self._tasks.items() if v["created_at"] < cutoff]:
                    self._tasks.pop(k, None)


store = TaskStore()
semaphore = asyncio.Semaphore(MAX_CONCURRENT)

# ═══════════════════════════════════════════════════════════════════════
# SOLVER LOGIC
# ═══════════════════════════════════════════════════════════════════════
def _solve_altcha(ch: AltchaChallenge) -> tuple[str, int]:
    max_n = min(ch.maxnumber, 5_000_000)
    for n in range(max_n):
        if hashlib.sha256(f"{ch.salt}{n}".encode()).hexdigest() == ch.challenge:
            solution = {
                "algorithm": ch.algorithm,
                "challenge": ch.challenge,
                "number": n,
                "salt": ch.salt,
            }
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


async def _solve_image_via_vision(image_b64: str, hint: Optional[str]) -> str:
    async with httpx.AsyncClient(timeout=SOLVE_TIMEOUT) as c:
        try:
            r = await c.post(
                f"{VISION_URL}/solve-text",
                json={"image": image_b64, "hint": hint or ""},
            )
        except httpx.ConnectError as e:
            raise RuntimeError(f"vision_unreachable: {e}")
        if r.status_code != 200:
            raise RuntimeError(f"vision_http_{r.status_code}: {r.text[:200]}")
        text = (r.json().get("text") or r.json().get("result") or "").strip()
        if not text:
            raise RuntimeError("vision_returned_empty")
        return text

# ═══════════════════════════════════════════════════════════════════════
# LIFESPAN
# ═══════════════════════════════════════════════════════════════════════
@asynccontextmanager
async def lifespan(app: FastAPI):
    await store.start()
    print(f"[app] started | vision_url={VISION_URL} auth={'on' if API_KEY else 'off'}", flush=True)
    yield
    await store.stop()


app = FastAPI(title="Fortress Captcha API", version="2.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

# ═══════════════════════════════════════════════════════════════════════
# AUTH (optional)
# ═══════════════════════════════════════════════════════════════════════
def require_api_key(request: Request):
    if not API_KEY:
        return
    provided = request.headers.get("x-api-key", "")
    if provided != API_KEY:
        raise HTTPException(401, "invalid_api_key")

# ═══════════════════════════════════════════════════════════════════════
# INFO + HEALTH
# ═══════════════════════════════════════════════════════════════════════
@app.get("/")
async def root():
    return {
        "service": "fortress-captcha",
        "version": "2.0.0",
        "auth_required": bool(API_KEY),
        "endpoints": {
            "sync":    ["POST /v1/solve/altcha", "POST /v1/solve/friendly"],
            "async":   ["POST /v1/solve/image"],
            "tasks":   ["GET /v1/tasks/{id}", "GET /v1/tasks/{id}/wait", "DELETE /v1/tasks/{id}"],
            "meta":    ["GET /health"],
        },
    }


@app.get("/health")
async def health():
    async with store._lock:
        pending = sum(1 for t in store._tasks.values() if t["status"] == "pending")
        done = sum(1 for t in store._tasks.values() if t["status"] == "done")
        failed = sum(1 for t in store._tasks.values() if t["status"] == "failed")
    return {
        "status": "ok",
        "tasks": {"total": len(store._tasks), "pending": pending, "done": done, "failed": failed},
        "slots_free": semaphore._value,
        "vision_url": VISION_URL,
        "ts": time.time(),
    }

# ═══════════════════════════════════════════════════════════════════════
# SYNC SOLVERS
# ═══════════════════════════════════════════════════════════════════════
@app.post("/v1/solve/altcha", response_model=AltchaResponse)
async def solve_altcha(req: AltchaRequest, _=Depends(require_api_key)):
    t0 = time.time()
    try:
        sol, iters = await asyncio.get_event_loop().run_in_executor(
            None, _solve_altcha, req.challenge)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return AltchaResponse(solution=sol, iterations=iters,
                          elapsed_ms=int((time.time() - t0) * 1000))


@app.post("/v1/solve/friendly", response_model=FriendlyResponse)
async def solve_friendly(req: FriendlyRequest, _=Depends(require_api_key)):
    t0 = time.time()
    try:
        sol, nonce = await asyncio.get_event_loop().run_in_executor(
            None, _solve_friendly, req.challenge, req.difficulty, req.max_nonce)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return FriendlyResponse(solution=sol, nonce=nonce,
                            elapsed_ms=int((time.time() - t0) * 1000))

# ═══════════════════════════════════════════════════════════════════════
# ASYNC — IMAGE
# ═══════════════════════════════════════════════════════════════════════
async def _run_image_task(tid: str, image_b64: str, hint: Optional[str]):
    async with semaphore:
        await store.update(tid, status="processing")
        t0 = time.time()
        try:
            result = await _solve_image_via_vision(image_b64, hint)
            await store.update(tid, status="done", result=result,
                               elapsed_ms=int((time.time() - t0) * 1000))
        except Exception as e:
            await store.update(tid, status="failed", error=str(e)[:500],
                               elapsed_ms=int((time.time() - t0) * 1000))


@app.post("/v1/solve/image", response_model=TaskResponse, status_code=202)
async def submit_image(req: ImageRequest, bg: BackgroundTasks,
                       _=Depends(require_api_key)):
    tid = await store.create("image")
    bg.add_task(_run_image_task, tid, req.image_base64, req.hint)
    return TaskResponse(**await store.get(tid))

# ═══════════════════════════════════════════════════════════════════════
# TASK MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════
@app.get("/v1/tasks/{tid}", response_model=TaskResponse)
async def get_task(tid: str, _=Depends(require_api_key)):
    t = await store.get(tid)
    if not t:
        raise HTTPException(404, "task_not_found")
    return TaskResponse(**t)


@app.get("/v1/tasks/{tid}/wait", response_model=TaskResponse)
async def wait_task(tid: str, timeout: int = 60, _=Depends(require_api_key)):
    if timeout > 300:
        raise HTTPException(400, "timeout_max_300")
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = await store.get(tid)
        if not t:
            raise HTTPException(404, "task_not_found")
        if t["status"] in ("done", "failed"):
            return TaskResponse(**t)
        await asyncio.sleep(0.5)
    t = await store.get(tid)
    if not t:
        raise HTTPException(404, "task_not_found")
    return TaskResponse(**t)


@app.delete("/v1/tasks/{tid}")
async def delete_task(tid: str, _=Depends(require_api_key)):
    if not await store.delete(tid):
        raise HTTPException(404, "task_not_found")
    return {"deleted": tid}
