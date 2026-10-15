#!/bin/sh
# ═══════════════════════════════════════════════════════════════════════
# Fortress Captcha — start.sh (Redis-free mode)
# - Jika REDIS_URL kosong: pakai in-memory queue + inline solvers
# - 2 service saja: api-gateway + vision-solver
# ═══════════════════════════════════════════════════════════════════════
set -e

SERVICE="${SERVICE:-api-gateway}"
echo "[start] SERVICE=$SERVICE PORT=${PORT:-unset} PWD=$(pwd)"

# ── Redis-free overrides ────────────────────────────────────────────
if [ -z "${REDIS_URL:-}" ]; then
    echo "[start] REDIS_URL empty → installing in-memory queue & inline solvers"

    mkdir -p /app/shared

    cat > /app/shared/queue.py <<'PYEOF'
"""In-memory task queue (no Redis)."""
import asyncio, time, uuid


class TaskQueue:
    def __init__(self, redis_url=None):
        self._tasks: dict = {}
        self._cleanup = None

    async def connect(self):
        self._cleanup = asyncio.create_task(self._cleanup_loop())

    async def enqueue(self, task_type, payload):
        tid = str(uuid.uuid4())
        tval = task_type if isinstance(task_type, str) else getattr(task_type, "value", str(task_type))
        self._tasks[tid] = {
            "id": tid, "type": tval, "status": "pending",
            "payload": payload, "created_at": time.time(), "attempts": 0,
        }
        return tid

    async def get(self, tid):
        return self._tasks.get(tid)

    async def update(self, tid, **kw):
        if tid in self._tasks:
            self._tasks[tid].update(kw)

    async def dequeue(self, *a, **kw): return None
    async def ack(self, *a, **kw): pass
    async def requeue(self, *a, **kw): pass
    async def move_to_dead_letter(self, *a, **kw): pass

    async def close(self):
        if self._cleanup:
            self._cleanup.cancel()

    async def _cleanup_loop(self):
        while True:
            await asyncio.sleep(60)
            now = time.time()
            for k in [k for k, v in self._tasks.items() if now - v["created_at"] > 3600]:
                self._tasks.pop(k, None)
PYEOF

    cat > /app/shared/redis_client.py <<'PYEOF'
"""Stub — no real Redis needed."""


class _FakeRedis:
    async def ping(self): return True
    async def xlen(self, *a, **kw): return 0
    async def aclose(self): pass


_r = None


async def get_redis():
    global _r
    if _r is None:
        _r = _FakeRedis()
    return _r


async def close_redis():
    global _r
    _r = None
PYEOF

    cat > /app/services/api-gateway/main.py <<'PYEOF'
"""API Gateway — in-memory queue + inline ALTCHA/Friendly solvers."""
import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from services.api_gateway.routes import router
from shared.logging import setup_logging, new_correlation_id
from shared.metrics import render_metrics
from shared.queue import TaskQueue
from shared.redis_client import get_redis, close_redis

setup_logging("api-gateway")


async def solve_one(task, s):
    import base64, hashlib, json, struct, httpx
    t = task["type"]
    p = task["payload"]

    if t == "task:alt":
        d = json.loads(p["challenge"]) if isinstance(p.get("challenge"), str) else p.get("challenge", {})
        salt, target = d["salt"], d["challenge"]
        maxn = min(int(d.get("maxnumber", 5_000_000)), 5_000_000)
        for n in range(maxn):
            if hashlib.sha256(f"{salt}{n}".encode()).hexdigest() == target:
                return base64.b64encode(json.dumps({
                    "algorithm": d.get("algorithm", "SHA-256"),
                    "challenge": target, "number": n, "salt": salt,
                }).encode()).decode()
        raise RuntimeError("no_solution")

    if t == "task:friendly":
        buf = base64.b64decode(p["challenge"])
        diff = int(p.get("difficulty", 16))
        threshold = 1 << (32 - diff)
        for nonce in range(10_000_000):
            b = buf + struct.pack(">Q", nonce)
            h = hashlib.blake2b(b, digest_size=32).digest()
            if struct.unpack("<I", h[:4])[0] < threshold:
                return base64.b64encode(b).decode()
        raise RuntimeError("no_solution")

    if t == "task:img":
        async with httpx.AsyncClient(timeout=180) as c:
            r = await c.post(f"{s.service_url('vision')}/solve-text", json=p)
            if r.status_code == 200:
                return r.json().get("text", "")
            raise RuntimeError(f"vision_{r.status_code}")

    raise RuntimeError(f"unsupported_type:{t}")


async def solver_loop(app):
    from shared.config import get_settings
    s = get_settings()
    q = app.state.queue
    while True:
        await asyncio.sleep(0.5)
        for tid, task in list(q._tasks.items()):
            if task.get("status") != "pending":
                continue
            await q.update(tid, status="processing")
            try:
                result = await asyncio.wait_for(solve_one(task, s), timeout=180)
                await q.update(tid, status="done", result=result)
            except Exception as e:
                await q.update(tid, status="failed", error=str(e)[:300])


@asynccontextmanager
async def lifespan(app):
    await get_redis()
    q = TaskQueue()
    await q.connect()
    app.state.queue = q
    asyncio.create_task(solver_loop(app))
    yield
    await q.close()
    await close_redis()


app = FastAPI(title="Fortress Captcha API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def cid_mw(request: Request, call_next):
    cid = request.headers.get("X-Correlation-ID") or new_correlation_id()
    resp = await call_next(request)
    resp.headers["X-Correlation-ID"] = cid
    return resp


app.include_router(router)


@app.get("/metrics")
async def metrics():
    body, ct = render_metrics()
    return Response(content=body, media_type=ct)
PYEOF

    echo "[start] in-memory overrides installed"
else
    echo "[start] REDIS_URL detected → using Redis queue (original mode)"
fi

# ── Dispatch ────────────────────────────────────────────────────────
case "$SERVICE" in
    api-gateway|gateway)
        cd /app/services/api-gateway
        exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8080}" --workers 1
        ;;

    vision|vision-service|vision-solver)
        MODEL_DIR="${QWEN_LOCAL_DIR:-/data/models/qwen}"
        if [ ! -f "$MODEL_DIR/.download-complete" ]; then
            echo "[start] downloading Qwen → $MODEL_DIR"
            python - <<'PYEOF' || echo "[start] WARN: model download failed"
import os, sys
from pathlib import Path
try:
    from huggingface_hub import snapshot_download
except ImportError:
    print("[dl] no huggingface_hub", file=sys.stderr); sys.exit(1)
dest = Path(os.environ.get("QWEN_LOCAL_DIR", "/data/models/qwen"))
dest.mkdir(parents=True, exist_ok=True)
repo = os.environ.get("QWEN_REPO", "Qwen/Qwen3-VL-2B-Instruct")
try:
    snapshot_download(repo_id=repo, local_dir=str(dest),
                      token=os.environ.get("HF_TOKEN") or None,
                      ignore_patterns=["*.msgpack","*.h5","*.ot","*.tflite","*.onnx"])
    (dest / ".download-complete").touch()
    print("[dl] OK")
except Exception as e:
    print(f"[dl] FAILED: {e}", file=sys.stderr); sys.exit(1)
PYEOF
        else
            echo "[start] model already at $MODEL_DIR"
        fi
        cd /app/services/vision-service
        exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-9002}" --workers 1
        ;;

    altcha|altcha-service)
        cd /app/services/altcha-service
        exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-9005}"
        ;;

    friendly|friendly-service)
        cd /app/services/friendly-service
        exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-9006}"
        ;;

    *)
        echo "[start] unknown SERVICE='$SERVICE'" >&2
        exit 1
        ;;
esac
