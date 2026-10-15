"""
Fortress Captcha API v3.1 - Full.

Changelog v3.1:
  - Fixed Turnstile regex extraction (7 patterns + fallback)
  - Added /v1/debug/flaresolverr endpoint for troubleshooting
  - HTML entity decode before regex matching
  - ASCII-safe source (no unicode)

Changelog v3.0:
  - FlareSolverr integration for Turnstile + reCAPTCHA v2
  - Legacy /solve support for all types

Endpoints:
  POST /solve                    -> legacy CapSkip-compatible
  POST /v1/solve/altcha          -> sync ALTCHA PoW
  POST /v1/solve/friendly        -> sync Friendly Captcha PoW
  POST /v1/solve/image           -> async image
  POST /v1/solve/turnstile       -> async Turnstile
  POST /v1/solve/recaptcha       -> async reCAPTCHA v2
  GET  /v1/tasks/{id}            -> task status
  GET  /v1/tasks/{id}/wait       -> long-poll
  DELETE /v1/tasks/{id}          -> delete
  GET  /v1/debug/flaresolverr    -> raw FlareSolverr HTML dump
  GET  /health                   -> health + dependency check
  GET  /                         -> API info
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import html as html_module
import json
import os
import re
import struct
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Literal, Optional

import httpx
from fastapi import BackgroundTasks, Body, Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field


# =====================================================================
# CONFIG
# =====================================================================
VISION_URL = os.getenv(
    "VISION_URL", "http://vision-solver.railway.internal:9002"
)
FLARESOLVERR_URL = os.getenv(
    "FLARESOLVERR_URL", "http://127.0.0.1:8191"
)
FLARESOLVERR_ENABLED = os.getenv(
    "FLARESOLVERR_ENABLED", "1"
).lower() in ("1", "true", "yes")

TASK_TTL = int(os.getenv("TASK_TTL_SECONDS", "3600"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_SOLVES", "8"))
SOLVE_TIMEOUT = int(os.getenv("SOLVE_TIMEOUT", "180"))
BROWSER_TIMEOUT = int(os.getenv("BROWSER_TIMEOUT", "120"))
API_KEY = os.getenv("API_KEY", "")


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
    sitekey: str = Field(..., description="Site key captcha")
    pageurl: str = Field(..., description="URL halaman berisi captcha")
    action: Optional[str] = None
    proxy: Optional[str] = None


class TaskResponse(BaseModel):
    id: str
    type: str
    status: Literal["pending", "processing", "done", "failed"]
    result: Optional[str] = None
    error: Optional[str] = None
    elapsed_ms: Optional[int] = None
    created_at: float
    updated_at: float


# =====================================================================
# TASK STORE
# =====================================================================
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
                for k in [
                    k for k, v in self._tasks.items()
                    if v["created_at"] < cutoff
                ]:
                    self._tasks.pop(k, None)


store = TaskStore()
semaphore = asyncio.Semaphore(MAX_CONCURRENT)


# =====================================================================
# POW SOLVERS
# =====================================================================
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
            return base64.b64encode(
                json.dumps(solution).encode()
            ).decode(), n
    raise ValueError(f"no_solution_found_within_{max_n}_iterations")


def _solve_friendly(challenge_b64: str, difficulty: int,
                    max_nonce: int) -> tuple[str, int]:
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
            r = await c.post(
                f"{VISION_URL}/solve-text",
                json={"image": image_b64, "hint": hint or ""},
            )
        except httpx.ConnectError as e:
            raise RuntimeError(f"vision_unreachable: {e}")
        except httpx.TimeoutException:
            raise RuntimeError(f"vision_timeout_after_{SOLVE_TIMEOUT}s")

        if r.status_code != 200:
            raise RuntimeError(
                f"vision_http_{r.status_code}: {r.text[:200]}"
            )
        data = r.json()
        text = (data.get("text") or data.get("result") or "").strip()
        if not text:
            raise RuntimeError("vision_returned_empty")
        return text


# =====================================================================
# FLARESOLVERR
# =====================================================================
async def _check_flaresolverr() -> tuple[bool, str]:
    if not FLARESOLVERR_ENABLED:
        return False, "disabled_by_env"
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{FLARESOLVERR_URL}/")
            if r.status_code < 500:
                return True, "reachable"
            return False, f"http_{r.status_code}"
    except httpx.ConnectError:
        return False, "connect_refused"
    except httpx.TimeoutException:
        return False, "timeout"
    except Exception as e:
        return False, f"error_{type(e).__name__}"


def _extract_turnstile_token(html: str) -> Optional[str]:
    """
    Extract Cloudflare Turnstile token from HTML.
    Tries 7 regex patterns then falls back to generic token scan.
    """
    decoded = html_module.unescape(html)
    patterns = [
        # 1. Hidden input standard
        r'<input[^>]*name=["\']cf-turnstile-response["\'][^>]*value=["\']([^"\']{20,})["\']',
        # 2. Value before name
        r'<input[^>]*value=["\']([^"\']{20,})["\'][^>]*name=["\']cf-turnstile-response["\']',
        # 3. Textarea
        r'<textarea[^>]*name=["\']cf-turnstile-response["\'][^>]*>([^<]{20,})</textarea>',
        # 4. JSON embedded
        r'["\']cf-turnstile-response["\']\s*[:=]\s*["\']([^"\']{20,})["\']',
        # 5. turnstileToken variable
        r'turnstileToken\s*[:=]\s*["\']([^"\']{20,})["\']',
        # 6. Generic token variable name
        r'(?:token|response|verify)\s*[:=]\s*["\'](0\.[A-Za-z0-9_\-\.]{80,})["\']',
        # 7. Cloudflare clearance ray
        r'cRay["\']?\s*[:=]\s*["\']([^"\']{20,})["\']',
    ]
    for pat in patterns:
        m = re.search(pat, decoded)
        if m:
            token = m.group(1).strip()
            if len(token) >= 30:
                return token

    # Fallback: scan all Turnstile-like tokens (0.xxx...)
    candidates = re.findall(
        r'["\'](0\.[A-Za-z0-9_\-]{80,})["\']', decoded
    )
    if candidates:
        return max(candidates, key=len)
    return None


def _extract_recaptcha_token(html: str) -> Optional[str]:
    """
    Extract reCAPTCHA v2 token from HTML.
    """
    decoded = html_module.unescape(html)
    patterns = [
        r'<textarea[^>]*name=["\']g-recaptcha-response["\'][^>]*>([^<]{20,})</textarea>',
        r'<textarea[^>]*id=["\']g-recaptcha-response["\'][^>]*>([^<]{20,})</textarea>',
        r'["\']g-recaptcha-response["\']\s*[:=]\s*["\']([^"\']{20,})["\']',
        r'<input[^>]*name=["\']g-recaptcha-response["\'][^>]*value=["\']([^"\']{20,})["\']',
    ]
    for pat in patterns:
        m = re.search(pat, decoded)
        if m:
            token = m.group(1).strip()
            if len(token) >= 30:
                return token

    # Fallback: scan all long base64-like tokens
    candidates = re.findall(
        r'["\']([A-Za-z0-9_\-]{100,})["\']', decoded
    )
    if candidates:
        return max(candidates, key=len)
    return None


async def _solve_via_flaresolverr(
    sitekey: str,
    pageurl: str,
    kind: Literal["turnstile", "recaptcha"],
    proxy: Optional[str] = None,
) -> str:
    """
    Solve Turnstile or reCAPTCHA v2 via FlareSolverr.
    """
    if not FLARESOLVERR_ENABLED:
        raise RuntimeError("flaresolverr_disabled")

    payload = {
        "cmd": "request.get",
        "url": pageurl,
        "maxTimeout": BROWSER_TIMEOUT * 1000,
    }
    if proxy:
        payload["proxy"] = {"url": proxy}

    async with httpx.AsyncClient(timeout=BROWSER_TIMEOUT + 30) as c:
        try:
            r = await c.post(
                f"{FLARESOLVERR_URL}/v1",
                json=payload,
            )
        except httpx.ConnectError as e:
            raise RuntimeError(f"flaresolverr_unreachable: {e}")
        except httpx.TimeoutException:
            raise RuntimeError(
                f"flaresolverr_timeout_after_{BROWSER_TIMEOUT}s"
            )

        if r.status_code != 200:
            raise RuntimeError(
                f"flaresolverr_http_{r.status_code}: {r.text[:200]}"
            )

        try:
            data = r.json()
        except Exception:
            raise RuntimeError(f"flaresolverr_bad_json: {r.text[:200]}")

        if data.get("status") != "ok":
            msg = data.get("message", "unknown_error")
            raise RuntimeError(f"flaresolverr_error: {msg}")

        html = data.get("solution", {}).get("response", "")
        if not html:
            raise RuntimeError("flaresolverr_empty_response")

        if kind == "turnstile":
            token = _extract_turnstile_token(html)
            if token:
                return token
            raise RuntimeError("turnstile_token_not_found_in_response")

        if kind == "recaptcha":
            token = _extract_recaptcha_token(html)
            if token:
                return token
            raise RuntimeError("recaptcha_token_not_found_in_response")

        raise RuntimeError(f"unsupported_kind_{kind}")


# =====================================================================
# LIFESPAN
# =====================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    await store.start()
    fs_ok, fs_detail = await _check_flaresolverr()
    print(
        f"[app] started | version=3.1.0 | "
        f"vision={VISION_URL} | "
        f"flaresolverr={FLARESOLVERR_URL} ({fs_detail}) | "
        f"auth={'on' if API_KEY else 'off'}",
        flush=True,
    )
    yield
    await store.stop()


app = FastAPI(
    title="Fortress Captcha API",
    version="3.1.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# =====================================================================
# AUTH
# =====================================================================
def require_api_key(request: Request):
    if not API_KEY:
        return
    provided = (
        request.headers.get("x-api-key", "")
        or request.headers.get("key", "")
    )
    if provided != API_KEY:
        raise HTTPException(401, "invalid_api_key")


# =====================================================================
# INFO + HEALTH
# =====================================================================
@app.get("/")
async def root():
    return {
        "service": "fortress-captcha",
        "version": "3.1.0",
        "auth_required": bool(API_KEY),
        "capabilities": {
            "altcha": True,
            "friendly": True,
            "image": True,
            "turnstile": FLARESOLVERR_ENABLED,
            "recaptcha": FLARESOLVERR_ENABLED,
        },
        "endpoints": {
            "legacy": ["POST /solve"],
            "sync": [
                "POST /v1/solve/altcha",
                "POST /v1/solve/friendly",
            ],
            "async": [
                "POST /v1/solve/image",
                "POST /v1/solve/turnstile",
                "POST /v1/solve/recaptcha",
            ],
            "tasks": [
                "GET /v1/tasks/{id}",
                "GET /v1/tasks/{id}/wait",
                "DELETE /v1/tasks/{id}",
            ],
            "debug": ["GET /v1/debug/flaresolverr?url=..."],
            "meta": ["GET /health"],
        },
    }


@app.get("/health")
async def health():
    async with store._lock:
        pending = sum(
            1 for t in store._tasks.values() if t["status"] == "pending"
        )
        done = sum(
            1 for t in store._tasks.values() if t["status"] == "done"
        )
        failed = sum(
            1 for t in store._tasks.values() if t["status"] == "failed"
        )

    fs_ok, fs_detail = await _check_flaresolverr()

    return {
        "status": "ok",
        "tasks": {
            "total": len(store._tasks),
            "pending": pending,
            "done": done,
            "failed": failed,
        },
        "slots_free": semaphore._value,
        "dependencies": {
            "vision": {"url": VISION_URL},
            "flaresolverr": {
                "url": FLARESOLVERR_URL,
                "reachable": fs_ok,
                "detail": fs_detail,
                "enabled": FLARESOLVERR_ENABLED,
            },
        },
        "ts": time.time(),
    }


# =====================================================================
# SYNC SOLVERS
# =====================================================================
@app.post("/v1/solve/altcha", response_model=AltchaResponse)
async def solve_altcha(req: AltchaRequest, _=Depends(require_api_key)):
    t0 = time.time()
    try:
        sol, iters = await asyncio.get_event_loop().run_in_executor(
            None, _solve_altcha, req.challenge
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    return AltchaResponse(
        solution=sol, iterations=iters,
        elapsed_ms=int((time.time() - t0) * 1000),
    )


@app.post("/v1/solve/friendly", response_model=FriendlyResponse)
async def solve_friendly(req: FriendlyRequest, _=Depends(require_api_key)):
    t0 = time.time()
    try:
        sol, nonce = await asyncio.get_event_loop().run_in_executor(
            None, _solve_friendly,
            req.challenge, req.difficulty, req.max_nonce,
        )
    except ValueError as e:
        raise HTTPException(422, str(e))
    return FriendlyResponse(
        solution=sol, nonce=nonce,
        elapsed_ms=int((time.time() - t0) * 1000),
    )


# =====================================================================
# BACKGROUND TASKS
# =====================================================================
async def _run_image_task(tid: str, image_b64: str, hint: Optional[str]):
    async with semaphore:
        await store.update(tid, status="processing")
        t0 = time.time()
        try:
            result = await _solve_image_via_vision(image_b64, hint)
            await store.update(
                tid, status="done", result=result,
                elapsed_ms=int((time.time() - t0) * 1000),
            )
        except Exception as e:
            await store.update(
                tid, status="failed", error=str(e)[:500],
                elapsed_ms=int((time.time() - t0) * 1000),
            )


async def _run_browser_task(
    tid: str, sitekey: str, pageurl: str,
    kind: Literal["turnstile", "recaptcha"],
    proxy: Optional[str] = None,
):
    async with semaphore:
        await store.update(tid, status="processing")
        t0 = time.time()
        try:
            token = await _solve_via_flaresolverr(
                sitekey, pageurl, kind, proxy
            )
            await store.update(
                tid, status="done", result=token,
                elapsed_ms=int((time.time() - t0) * 1000),
            )
        except Exception as e:
            await store.update(
                tid, status="failed", error=str(e)[:500],
                elapsed_ms=int((time.time() - t0) * 1000),
            )


# =====================================================================
# ASYNC ENDPOINTS
# =====================================================================
@app.post("/v1/solve/image", response_model=TaskResponse, status_code=202)
async def submit_image(
    req: ImageRequest, bg: BackgroundTasks,
    _=Depends(require_api_key),
):
    tid = await store.create("image")
    bg.add_task(_run_image_task, tid, req.image_base64, req.hint)
    return TaskResponse(**await store.get(tid))


@app.post("/v1/solve/turnstile", response_model=TaskResponse, status_code=202)
async def submit_turnstile(
    req: BrowserRequest, bg: BackgroundTasks,
    _=Depends(require_api_key),
):
    if not FLARESOLVERR_ENABLED:
        raise HTTPException(503, "flaresolverr_disabled")
    if not req.sitekey or not req.pageurl:
        raise HTTPException(400, "sitekey_and_pageurl_required")
    tid = await store.create("turnstile")
    bg.add_task(
        _run_browser_task,
        tid, req.sitekey, req.pageurl, "turnstile", req.proxy,
    )
    return TaskResponse(**await store.get(tid))


@app.post("/v1/solve/recaptcha", response_model=TaskResponse, status_code=202)
async def submit_recaptcha(
    req: BrowserRequest, bg: BackgroundTasks,
    _=Depends(require_api_key),
):
    if not FLARESOLVERR_ENABLED:
        raise HTTPException(503, "flaresolverr_disabled")
    if not req.sitekey or not req.pageurl:
        raise HTTPException(400, "sitekey_and_pageurl_required")
    tid = await store.create("recaptcha")
    bg.add_task(
        _run_browser_task,
        tid, req.sitekey, req.pageurl, "recaptcha", req.proxy,
    )
    return TaskResponse(**await store.get(tid))


# =====================================================================
# TASK MANAGEMENT
# =====================================================================
@app.get("/v1/tasks/{tid}", response_model=TaskResponse)
async def get_task(tid: str, _=Depends(require_api_key)):
    t = await store.get(tid)
    if not t:
        raise HTTPException(404, "task_not_found")
    return TaskResponse(**t)


@app.get("/v1/tasks/{tid}/wait", response_model=TaskResponse)
async def wait_task(
    tid: str, timeout: int = 60,
    _=Depends(require_api_key),
):
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


# =====================================================================
# DEBUG
# =====================================================================
@app.get("/v1/debug/flaresolverr")
async def debug_flaresolverr(
    url: str,
    max_bytes: int = 50000,
    _=Depends(require_api_key),
):
    """
    Fetch URL via FlareSolverr and return raw HTML for troubleshooting.
    Example: /v1/debug/flaresolverr?url=https://example.com
    """
    if not FLARESOLVERR_ENABLED:
        raise HTTPException(503, "flaresolverr_disabled")

    async with httpx.AsyncClient(timeout=BROWSER_TIMEOUT + 30) as c:
        try:
            r = await c.post(
                f"{FLARESOLVERR_URL}/v1",
                json={
                    "cmd": "request.get",
                    "url": url,
                    "maxTimeout": BROWSER_TIMEOUT * 1000,
                },
            )
        except Exception as e:
            raise HTTPException(500, f"flaresolverr_failed: {e}")

        if r.status_code != 200:
            raise HTTPException(
                500, f"http_{r.status_code}: {r.text[:200]}"
            )

        data = r.json()
        html = data.get("solution", {}).get("response", "")
        cookies = data.get("solution", {}).get("cookies", [])

        # Keyword scan
        keywords = [
            "cf-turnstile", "turnstile",
            "cf_chl", "challenge-platform",
            "g-recaptcha", "recaptcha",
            "cf-turnstile-response", "g-recaptcha-response",
        ]
        found = {}
        for kw in keywords:
            idx = html.lower().find(kw.lower())
            if idx >= 0:
                start = max(0, idx - 80)
                end = min(len(html), idx + 200)
                found[kw] = {
                    "position": idx,
                    "context": html[start:end],
                }

        return {
            "url": url,
            "flaresolverr_status": data.get("status", "unknown"),
            "flaresolverr_message": data.get("message", ""),
            "html_length": len(html),
            "html_sample": html[:max_bytes],
            "cookies_count": len(cookies),
            "cookies_names": [c.get("name") for c in cookies],
            "keywords_found": found,
        }


# =====================================================================
# LEGACY /solve
# =====================================================================
_legacy_tasks: dict[str, dict[str, Any]] = {}
_legacy_lock = asyncio.Lock()


def _legacy_response(tid: str, task: dict) -> dict:
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
    CapSkip-compatible endpoint for legacy scripts.

    Poll mode:    {"taskId": "..."}
    Submit mode:  {"type": "...", ...}
    """
    # Poll mode
    if "taskId" in body:
        tid = str(body["taskId"])
        async with _legacy_lock:
            t = _legacy_tasks.get(tid)
        if not t:
            t = await store.get(tid)
        if not t:
            return {
                "status": "error", "error": "task_not_found",
                "message": "task id not found",
            }
        return _legacy_response(tid, t)

    ttype = (body.get("type") or "").lower()
    challenge_raw = (
        body.get("challenge")
        or body.get("challengeData")
        or body.get("data")
    )

    # ALTCHA
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
            ch = (
                challenge_raw
                if isinstance(challenge_raw, dict)
                else json.loads(challenge_raw)
            )
            req = AltchaChallenge(**ch)
            sol, _ = await asyncio.get_event_loop().run_in_executor(
                None, _solve_altcha, req
            )
            async with _legacy_lock:
                _legacy_tasks[tid].update(status="done", result=sol)
        except Exception as e:
            async with _legacy_lock:
                _legacy_tasks[tid].update(
                    status="failed", error=str(e)[:300]
                )
        async with _legacy_lock:
            task = dict(_legacy_tasks[tid])
        return _legacy_response(tid, task)

    # Friendly
    if ttype == "friendly" or (
        isinstance(challenge_raw, str)
        and ttype in ("", "friendly")
        and challenge_raw
    ):
        tid = str(uuid.uuid4())
        async with _legacy_lock:
            _legacy_tasks[tid] = {
                "id": tid, "type": "friendly", "status": "pending",
                "result": None, "error": None,
            }
        try:
            ch_b64 = (
                challenge_raw
                if isinstance(challenge_raw, str)
                else json.dumps(challenge_raw)
            )
            diff = int(body.get("difficulty") or 16)
            max_nonce = int(
                body.get("max_nonce") or body.get("maxNonce") or 10_000_000
            )
            sol, _ = await asyncio.get_event_loop().run_in_executor(
                None, _solve_friendly, ch_b64, diff, max_nonce
            )
            async with _legacy_lock:
                _legacy_tasks[tid].update(status="done", result=sol)
        except Exception as e:
            async with _legacy_lock:
                _legacy_tasks[tid].update(
                    status="failed", error=str(e)[:300]
                )
        async with _legacy_lock:
            task = dict(_legacy_tasks[tid])
        return _legacy_response(tid, task)

    # Image
    image_b64 = (
        body.get("image")
        or body.get("imageBase64")
        or body.get("image_base64")
        or body.get("body")
    )
    if ttype in ("image", "base64", "normal") or (
        image_b64 and ttype not in ("turnstile", "recaptcha", "hcaptcha")
    ):
        if not image_b64:
            return {
                "taskId": None, "status": "error",
                "error": "missing_image",
                "message": "image field required",
            }
        tid = await store.create("image")
        asyncio.create_task(
            _run_image_task(tid, image_b64, body.get("hint"))
        )
        return {"taskId": tid, "status": "pending"}

    # Turnstile
    if ttype == "turnstile":
        sitekey = (
            body.get("siteKey")
            or body.get("sitekey")
            or body.get("googlekey")
            or ""
        )
        pageurl = (
            body.get("domain")
            or body.get("url")
            or body.get("pageurl")
            or ""
        )
        if not sitekey or not pageurl:
            return {
                "taskId": None, "status": "error",
                "error": "missing_params",
                "message": "siteKey dan domain wajib diisi",
            }
        if not FLARESOLVERR_ENABLED:
            return {
                "taskId": None, "status": "error",
                "error": "flaresolverr_disabled",
                "message": "Set FLARESOLVERR_ENABLED=1",
            }
        tid = await store.create("turnstile")
        asyncio.create_task(
            _run_browser_task(
                tid, sitekey, pageurl, "turnstile", body.get("proxy")
            )
        )
        return {"taskId": tid, "status": "pending"}

    # reCAPTCHA v2
    if ttype in ("recaptcha", "userrecaptcha", "recaptcha_v2"):
        sitekey = (
            body.get("siteKey")
            or body.get("sitekey")
            or body.get("googlekey")
            or ""
        )
        pageurl = (
            body.get("domain")
            or body.get("url")
            or body.get("pageurl")
            or ""
        )
        if not sitekey or not pageurl:
            return {
                "taskId": None, "status": "error",
                "error": "missing_params",
                "message": "siteKey dan domain wajib diisi",
            }
        if not FLARESOLVERR_ENABLED:
            return {
                "taskId": None, "status": "error",
                "error": "flaresolverr_disabled",
                "message": "Set FLARESOLVERR_ENABLED=1",
            }
        tid = await store.create("recaptcha")
        asyncio.create_task(
            _run_browser_task(
                tid, sitekey, pageurl, "recaptcha", body.get("proxy")
            )
        )
        return {"taskId": tid, "status": "pending"}

    # Unsupported
    if ttype in ("hcaptcha", "recaptcha_v3"):
        return {
            "taskId": None, "status": "error",
            "error": "unsupported_type",
            "message": f"'{ttype}' belum didukung.",
        }

    return {
        "taskId": None, "status": "error",
        "error": "unknown_type",
        "message": f"unsupported type: '{ttype}'. "
                   f"Valid: altcha, friendly, image, turnstile, recaptcha",
    }


# =====================================================================
# TEST PAGE
# =====================================================================
@app.get("/test", include_in_schema=False)
async def test_page():
    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=(
        "<!doctype html><html><head><title>Fortress v3.1</title></head>"
        "<body><h1>Fortress Captcha v3.1</h1>"
        "<p>ALTCHA / Friendly / Image / Turnstile / reCAPTCHA</p>"
        "<p><a href='/health'>/health</a> | "
        "<a href='/'>/</a> | "
        "<a href='/docs'>/docs</a></p>"
        "</body></html>"
    ))


# =====================================================================
# DIRECT RUN
# =====================================================================
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, log_level="info")
