"""
Fortress Captcha API v2.1 — Self-contained, modern REST + legacy compat.

Features:
  - Modern REST API (JSON)
  - Legacy /solve endpoint (CapSkip-compatible, untuk script lama)
  - In-memory task queue (no Redis)
  - Inline ALTCHA + Friendly PoW solvers
  - Optional image solver via vision-solver microservice
  - Optional API key auth (X-API-Key header)

Endpoints:
  POST /solve                    → legacy submit or poll
  POST /v1/solve/altcha          → sync ALTCHA
  POST /v1/solve/friendly        → sync Friendly Captcha
  POST /v1/solve/image           → async image (vision-solver)
  GET  /v1/tasks/{id}            → task status
  GET  /v1/tasks/{id}/wait       → long-poll
  DELETE /v1/tasks/{id}          → delete task
  GET  /health                   → health check
  GET  /                         → API info
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
from fastapi import BackgroundTasks, Body, Depends, FastAPI, HTTPException, Request
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
# MODELS — MODERN API
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
        data = r.json()
        text = (data.get("text") or data.get("result") or "").strip()
        if not text:
            raise RuntimeError("vision_returned_empty")
        return text


# ═══════════════════════════════════════════════════════════════════════
# LIFESPAN
# ═══════════════════════════════════════════════════════════════════════
@asynccontextmanager
async def lifespan(app: FastAPI):
    await store.start()
    print(f"[app] started | vision_url={VISION_URL} auth={'on' if API_KEY else 'off'}",
          flush=True)
    yield
    await store.stop()


app = FastAPI(title="Fortress Captcha API", version="2.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


# ═══════════════════════════════════════════════════════════════════════
# AUTH (optional)
# ═══════════════════════════════════════════════════════════════════════
def require_api_key(request: Request):
    if not API_KEY:
        return
    provided = request.headers.get("x-api-key", "") or request.headers.get("key", "")
    if provided != API_KEY:
        raise HTTPException(401, "invalid_api_key")


# ═══════════════════════════════════════════════════════════════════════
# INFO + HEALTH
# ═══════════════════════════════════════════════════════════════════════
@app.get("/")
async def root():
    return {
        "service": "fortress-captcha",
        "version": "2.1.0",
        "auth_required": bool(API_KEY),
        "endpoints": {
            "legacy":  ["POST /solve"],
            "sync":    ["POST /v1/solve/altcha", "POST /v1/solve/friendly"],
            "async":   ["POST /v1/solve/image"],
            "tasks":   ["GET /v1/tasks/{id}",
                        "GET /v1/tasks/{id}/wait",
                        "DELETE /v1/tasks/{id}"],
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
        "tasks": {
            "total": len(store._tasks),
            "pending": pending, "done": done, "failed": failed,
        },
        "slots_free": semaphore._value,
        "vision_url": VISION_URL,
        "ts": time.time(),
    }


# ═══════════════════════════════════════════════════════════════════════
# SYNC SOLVERS — MODERN
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
# ASYNC IMAGE
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


# ═══════════════════════════════════════════════════════════════════════
# LEGACY COMPAT — POST /solve  (CapSkip-style)
#
# Submit:  {"type": "altcha",    "challenge": {...}}       → {"taskId": "..."}
#          {"type": "friendly",  "challenge": "<base64>"}  → {"taskId": "..."}
#          {"type": "image",     "image": "<base64>"}      → {"taskId": "..."}
# Poll:    {"taskId": "..."}                               → {"status": "done",
#                                                             "token": "..."}
# ═══════════════════════════════════════════════════════════════════════
_legacy_tasks: dict[str, dict[str, Any]] = {}
_legacy_lock = asyncio.Lock()


def _legacy_response(tid: str, task: dict) -> dict:
    """Convert internal task dict ke format legacy CapSkip."""
    status = task.get("status", "pending")
    if status == "done":
        result = task.get("result") or ""
        return {
            "taskId": tid,
            "status": "done",
            "token": result,
            "solution": {"token": result},
        }
    if status == "failed":
        return {
            "taskId": tid,
            "status": "error",
            "error": task.get("error") or "failed",
        }
    return {"taskId": tid, "status": status}


@app.post("/solve")
async def legacy_solve(body: dict = Body(...)):
    """
    CapSkip-compatible endpoint untuk script lama (lite-fx9.py, dll).

    Dua mode:
      1. POLL   — body {"taskId": "..."}      → return status
      2. SUBMIT — body {"type": "...", ...}   → return {"taskId": "..."}
    """
    # ─── POLL MODE ─────────────────────────────────────────────────
    if "taskId" in body:
        tid = str(body["taskId"])
        # Cek di legacy store dulu, lalu di modern store
        async with _legacy_lock:
            t = _legacy_tasks.get(tid)
        if not t:
            t = await store.get(tid)
        if not t:
            return {"status": "error", "error": "task_not_found",
                    "message": "task id not found"}
        return _legacy_response(tid, t)

    # ─── SUBMIT MODE ───────────────────────────────────────────────
    ttype = (body.get("type") or "").lower()

    # Ambil challenge dari berbagai nama field yang mungkin dikirim client
    challenge_raw = (
        body.get("challenge")
        or body.get("challengeData")
        or body.get("data")
    )

    # ── ALTCHA ────────────────────────────────────────────────────
    if ttype == "altcha" or (
        isinstance(challenge_raw, dict) and "salt" in challenge_raw
    ):
        tid = str(uuid.uuid4())
        async with _legacy_lock:
            _legacy_tasks[tid] = {
                "id": tid, "type": "altcha", "status": "pending",
                "result": None, "error": None,
            }
        try:
            ch = challenge_raw if isinstance(challenge_raw, dict) \
                else json.loads(challenge_raw)
            req = AltchaChallenge(**ch)
            sol, _ = await asyncio.get_event_loop().run_in_executor(
                None, _solve_altcha, req)
            async with _legacy_lock:
                _legacy_tasks[tid].update(status="done", result=sol)
        except Exception as e:
            async with _legacy_lock:
                _legacy_tasks[tid].update(status="failed", error=str(e)[:300])

        async with _legacy_lock:
            task = dict(_legacy_tasks[tid])
        return _legacy_response(tid, task)

    # ── FRIENDLY CAPTCHA ──────────────────────────────────────────
    if ttype == "friendly" or (
        isinstance(challenge_raw, str) and ttype in ("", "friendly")
    ):
        tid = str(uuid.uuid4())
        async with _legacy_lock:
            _legacy_tasks[tid] = {
                "id": tid, "type": "friendly", "status": "pending",
                "result": None, "error": None,
            }
        try:
            ch_b64 = challenge_raw if isinstance(challenge_raw, str) \
                else json.dumps(challenge_raw)
            diff = int(body.get("difficulty") or 16)
            max_nonce = int(body.get("max_nonce") or body.get("maxNonce") or 10_000_000)
            sol, _ = await asyncio.get_event_loop().run_in_executor(
                None, _solve_friendly, ch_b64, diff, max_nonce)
            async with _legacy_lock:
                _legacy_tasks[tid].update(status="done", result=sol)
        except Exception as e:
            async with _legacy_lock:
                _legacy_tasks[tid].update(status="failed", error=str(e)[:300])

        async with _legacy_lock:
            task = dict(_legacy_tasks[tid])
        return _legacy_response(tid, task)

    # ── IMAGE (async via vision-solver) ───────────────────────────
    image_b64 = (
        body.get("image")
        or body.get("imageBase64")
        or body.get("image_base64")
        or body.get("body")
    )
    if ttype in ("image", "base64", "normal") or image_b64:
        if not image_b64:
            return {"taskId": None, "status": "error",
                    "error": "missing_image",
                    "message": "image field required"}
        tid = await store.create("image")
        asyncio.create_task(_run_image_task(tid, image_b64, body.get("hint")))
        return {"taskId": tid, "status": "pending"}

    # ── TURNSTILE / RECAPTCHA / HCAPTCHA — TIDAK DIDUKUNG ─────────
    if ttype in ("turnstile", "recaptcha", "hcaptcha",
                 "userrecaptcha", "recaptcha_v2", "recaptcha_v3"):
        return {
            "taskId": None,
            "status": "error",
            "error": "unsupported_type",
            "message": (
                f"'{ttype}' membutuhkan browser-based solver yang tidak "
                f"tersedia di API ini. Gunakan 'altcha', 'friendly', "
                f"atau 'image'."
            ),
        }

    # ── UNKNOWN ───────────────────────────────────────────────────
    return {
        "taskId": None,
        "status": "error",
        "error": "unknown_type",
        "message": f"unsupported type: '{ttype}'. "
                   f"Valid: altcha, friendly, image",
    }


# ═══════════════════════════════════════════════════════════════════════
# OPTIONAL — inline test page
# ═══════════════════════════════════════════════════════════════════════
@app.get("/test", include_in_schema=False)
async def test_page():
    from fastapi.responses import HTMLResponse
    return HTMLResponse(content="""<!doctype html>
<html><head><title>Fortress Captcha — test</title>
<style>body{font-family:monospace;max-width:800px;margin:2em auto;padding:0 1em}
pre{background:#f4f4f4;padding:1em;overflow-x:auto;border-radius:4px}
button{padding:.5em 1em;margin:.3em}</style></head>
<body>
<h1>Fortress Captcha API v2.1</h1>
<p>Self-contained solver. No Redis, no API key (unless set).</p>
<h2>Endpoints</h2>
<ul>
<li><code>POST /solve</code> — legacy compat</li>
<li><code>POST /v1/solve/altcha</code></li>
<li><code>POST /v1/solve/friendly</code></li>
<li><code>POST /v1/solve/image</code></li>
<li><code>GET /v1/tasks/{id}</code></li>
<li><code>GET /health</code></li>
</ul>
<button onclick="ping()">Ping /health</button>
<pre id="out">click button…</pre>
<script>
async function ping(){
  const r = await fetch('/health').then(x=>x.json());
  document.getElementById('out').textContent = JSON.stringify(r,null,2);
}
</script>
</body></html>""")


# ═══════════════════════════════════════════════════════════════════════
# ENTRYPOINT (for `python app.py` direct run — optional)
# ═══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, log_level="info")
