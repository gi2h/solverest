# app.py — Fortress Captcha 4.2
import asyncio
import os
import time
import uuid
from typing import Dict, List, Optional, Union

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from turnstile_solver1 import solve_turnstile


# ─────────────────────────────────────────────────────────────
# Config (env-overridable)
# ─────────────────────────────────────────────────────────────
API_KEY         = os.environ.get("SOLVER_API_KEY", "").strip()
MAX_CONCURRENT  = int(os.environ.get("MAX_CONCURRENT", "2"))   # browser paralel
MAX_QUEUE       = int(os.environ.get("MAX_QUEUE", "6"))        # antrian tunggu
TASK_TIMEOUT    = int(os.environ.get("SOLVER_TIMEOUT", "90"))
RESULT_TTL      = int(os.environ.get("RESULT_TTL", "60"))      # detik
ORPHAN_TTL      = int(os.environ.get("ORPHAN_TTL", "600"))     # pending/running terlalu lama
DELETE_ON_FETCH = os.environ.get("DELETE_ON_FETCH", "true").lower() == "true"
DELETE_GRACE    = float(os.environ.get("DELETE_GRACE", "2.0"))


app = FastAPI(title="Fortress Captcha", version="4.2")

TASKS: Dict[str, dict] = {}
ASYNCIO_TASKS: Dict[str, asyncio.Task] = {}
SEMAPHORE: Optional[asyncio.Semaphore] = None


# ─────────────────────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────────────────────
class SolveRequest(BaseModel):
    sitekey: str = ""
    pageurl: str = Field(..., description="URL halaman target")
    type: Optional[str] = None           # "turnstile" — diterima, diabaikan
    proxy: Optional[str] = None
    cookies: Optional[Union[str, List[dict]]] = None
    user_agent: Optional[str] = None
    timeout: int = TASK_TIMEOUT
    headless: Optional[bool] = None


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
def _check_api_key(x_api_key: Optional[str]):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(401, "invalid api key")


def _active_count() -> int:
    return sum(1 for t in TASKS.values() if t["status"] in ("pending", "running"))


def _purge_task(task_id: str) -> bool:
    """Hancurkan task total: cancel kalau masih jalan, hapus dari memory."""
    existed = TASKS.pop(task_id, None) is not None
    at = ASYNCIO_TASKS.pop(task_id, None)
    if at and not at.done():
        at.cancel()
    return existed


async def _delayed_purge(task_id: str, delay: float):
    try:
        await asyncio.sleep(delay)
        _purge_task(task_id)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# Lifecycle
# ─────────────────────────────────────────────────────────────
@app.on_event("startup")
async def _startup():
    global SEMAPHORE
    SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT)
    asyncio.create_task(_cleanup_loop())


@app.on_event("shutdown")
async def _shutdown():
    for tid in list(ASYNCIO_TASKS.keys()):
        _purge_task(tid)


async def _cleanup_loop():
    """Bersihkan task yang sudah tidak perlu secara berkala."""
    while True:
        try:
            await asyncio.sleep(15)
            now = time.time()
            for tid, t in list(TASKS.items()):
                status = t["status"]
                # final state → hapus setelah RESULT_TTL
                if status in ("done", "failed") and t.get("finished_at"):
                    if now - t["finished_at"] > RESULT_TTL:
                        _purge_task(tid)
                        continue
                # pending/running terlalu lama → orphan
                if status in ("pending", "running"):
                    if now - t["created_at"] > ORPHAN_TTL:
                        _purge_task(tid)
                        continue
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────
async def _run_task(task_id: str, req: SolveRequest):
    task = TASKS.get(task_id)
    if task is None:
        return

    try:
        # Antrian sebenarnya di sini — baru mulai saat slot tersedia
        async with SEMAPHORE:
            # Cek apakah task sudah dihapus selama menunggu
            if TASKS.get(task_id) is not task:
                return

            task["status"] = "running"
            task["started_at"] = time.time()

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
                task["result"] = result.get("token")
                task["error"]  = None
            else:
                task["status"] = "failed"
                task["result"] = None
                task["error"]  = result.get("error", "unknown_error")
            task["finished_at"] = time.time()

    except asyncio.CancelledError:
        if TASKS.get(task_id) is task:
            task["status"] = "failed"
            task["error"]  = "cancelled"
            task["finished_at"] = time.time()
        raise
    except Exception as e:
        if TASKS.get(task_id) is task:
            task["status"] = "failed"
            task["result"] = None
            task["error"]  = f"{type(e).__name__}: {e}"
            task["finished_at"] = time.time()


# ─────────────────────────────────────────────────────────────
# Submit
# ─────────────────────────────────────────────────────────────
def _submit(req: SolveRequest) -> dict:
    capacity = MAX_CONCURRENT + MAX_QUEUE
    active = _active_count()
    if active >= capacity:
        raise HTTPException(
            429,
            detail={
                "error": "queue_full",
                "active": active,
                "max_concurrent": MAX_CONCURRENT,
                "max_queue": MAX_QUEUE,
                "capacity": capacity,
            },
        )

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
    ASYNCIO_TASKS[task_id] = asyncio.create_task(_run_task(task_id, req))
    return {
        "id": task_id,
        "task_id": task_id,
        "taskId": task_id,
        "status": "pending",
        "queue_position": active + 1,
    }


# ─────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    active = _active_count()
    return {
        "status": "ok",
        "version": app.version,
        "tasks_in_memory": len(TASKS),
        "active": active,
        "capacity": MAX_CONCURRENT + MAX_QUEUE,
        "max_concurrent": MAX_CONCURRENT,
        "max_queue": MAX_QUEUE,
    }


@app.get("/")
async def root():
    return {
        "name": "Fortress Captcha",
        "version": app.version,
        "concurrency": {
            "max_concurrent": MAX_CONCURRENT,
            "max_queue": MAX_QUEUE,
            "capacity": MAX_CONCURRENT + MAX_QUEUE,
        },
        "endpoints": [
            "POST   /v1/solve/turnstile",
            "POST   /v1/tasks",
            "GET    /v1/tasks/{id}",
            "GET    /v1/tasks/{id}/wait?timeout=5",
            "DELETE /v1/tasks/{id}",
            "DELETE /v1/tasks",
            "GET    /health",
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
        raise HTTPException(404, "task not found")
    snapshot = dict(task)
    # Hancurkan setelah diambil (dengan grace kecil untuk retry client)
    if snapshot["status"] in ("done", "failed") and DELETE_ON_FETCH:
        asyncio.create_task(_delayed_purge(task_id, DELETE_GRACE))
    return snapshot


@app.get("/v1/tasks/{task_id}/wait")
async def wait_task(task_id: str, timeout: int = 5):
    task = TASKS.get(task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    timeout = max(1, min(timeout, 60))
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if task["status"] in ("done", "failed"):
            break
        await asyncio.sleep(0.2)
    snapshot = dict(task)
    if snapshot["status"] in ("done", "failed") and DELETE_ON_FETCH:
        asyncio.create_task(_delayed_purge(task_id, DELETE_GRACE))
    return snapshot


@app.delete("/v1/tasks/{task_id}")
async def delete_task(task_id: str):
    if not _purge_task(task_id):
        raise HTTPException(404, "task not found")
    return {"deleted": task_id}


@app.delete("/v1/tasks")
async def purge_finished():
    """Hancurkan semua task yang sudah final (done/failed)."""
    n = 0
    for tid, t in list(TASKS.items()):
        if t["status"] in ("done", "failed"):
            if _purge_task(tid):
                n += 1
    return {"purged": n, "remaining": len(TASKS)}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
