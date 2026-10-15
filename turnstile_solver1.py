#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Turnstile/ReCaptcha solver service for Railway.
Drop-in replacement for the previous turnstile_solver.py.

Key fix vs. prior version:
  Chromium's --proxy-server flag DROPS inline user:pass@ credentials.
  Playwright/patchright must be given username & password as SEPARATE keys
  so it can answer the proxy's 407 via CDP authRequired.
"""

import asyncio
import os
import time
import uuid
import logging
from typing import Optional, Dict, Any
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
import uvicorn

from patchright.async_api import async_playwright

# ---------------------------------------------------------------- logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("solver")

# ---------------------------------------------------------------- config
HEADLESS = os.getenv("SOLVER_HEADLESS", "true").lower() == "true"
NAV_TIMEOUT_MS = int(os.getenv("SOLVER_NAV_TIMEOUT_MS", "45000"))
SOLVE_TIMEOUT_S = int(os.getenv("SOLVER_SOLVE_TIMEOUT_S", "120"))
API_KEY = os.getenv("SOLVER_API_KEY", "")

# ---------------------------------------------------------------- stealth
STEALTH_JS = r"""
// --- PFVisitCheck / adblock-shield bypass for pandafaucet.com ---
(() => {
  try {
    if (typeof window.PFVisitCheck !== 'undefined') return;
  } catch (e) {}
  window.PFVisitCheck = {
    ok: true,
    isOk: () => true,
    isFine: () => true,
    check: () => true,
    verify: () => true,
    passed: true,
    status: 'ok',
    result: true,
    isBot: false,
    isHuman: true,
    adblock: false,
    adBlockDetected: false,
    blockers: [],
    detected: [],
    visitCheck: true,
    visit_check: true,
    checkVisit: () => true,
    verifyVisit: () => true,
    verify: async () => true,
    run: async () => true,
    init: () => true,
    // network beacon the site usually waits for
    report: () => true,
    sendBeacon: () => true,
  };

  // Neutralize common adblock bait selectors
  const _origCreateElement = document.createElement.bind(document);
  document.createElement = function (tag, ...rest) {
    const el = _origCreateElement(tag, ...rest);
    if (typeof tag === 'string' && /bait|ad|ads|advert|banner|sponsor/i.test(tag)) {
      try { el.offsetHeight = 1; el.offsetWidth = 1; } catch (_) {}
    }
    return el;
  };

  // Hide webdriver flag (belt + suspenders; patchright already handles it)
  try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); } catch (_) {}
})();
"""

# ---------------------------------------------------------------- proxy fix
def _proxy_opts(proxy: str) -> Dict[str, str]:
    """
    Chromium drops user:pass@ from --proxy-server.
    Playwright must receive username/password as SEPARATE keys so it can
    answer the 407 via CDP Fetch.authRequired.

    Example:
        http://user:pass@host:80
      -> {"server": "http://host:80", "username": "user", "password": "pass"}
    """
    if not proxy:
        return {}
    if "://" not in proxy:
        proxy = "http://" + proxy
    u = urlparse(proxy)
    if not u.hostname or not u.port:
        # Let Playwright try to parse it if it's malformed
        return {"server": proxy}
    opts = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        opts["username"] = u.username
    if u.password:
        opts["password"] = u.password
    return opts


# ---------------------------------------------------------------- task store
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


async def _task_update(tid: str, status: str,
                       result: Optional[str] = None,
                       error: Optional[str] = None) -> None:
    async with TASKS_LOCK:
        t = TASKS.get(tid)
        if not t:
            return
        t.status = status
        t.result = result
        t.error = error
        t.updated_at = time.time()


# ---------------------------------------------------------------- browser
async def _launch_browser(playwright, proxy: Optional[str]):
    """Launch patchright Chromium with proxy auth correctly wired."""
    launch_opts: Dict[str, Any] = {
        "headless": HEADLESS,
        "args": [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
        ],
    }

    if proxy:
        opts = _proxy_opts(proxy)
        if opts:
            launch_opts["proxy"] = opts
            # Redact password before logging
            safe = {**opts}
            if "password" in safe:
                safe["password"] = "***"
            log.info("launch proxy=%s", safe)

    browser = await playwright.chromium.launch(**launch_opts)
    return browser


# ---------------------------------------------------------------- turnstile
async def _solve_turnstile(sitekey: str, pageurl: str,
                           proxy: Optional[str]) -> Optional[str]:
    async with async_playwright() as pw:
        browser = await _launch_browser(pw, proxy)
        try:
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Linux; Android 13; 22120RN86G "
                    "Build/TQ3A.230901.001) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/138.0.7204.63 "
                    "Mobile Safari/537.36"
                ),
                viewport={"width": 412, "height": 915},
                locale="en-US",
            )
            await context.add_init_script(STEALTH_JS)

            page = await context.new_page()

            # -------- sanity: verify Chromium really goes through the proxy
            try:
                await page.goto("https://api.ipify.org",
                                wait_until="domcontentloaded",
                                timeout=15000)
                egress = (await page.inner_text("body")).strip()
                log.info("turnstile: egress ip = %s", egress)
            except Exception as e:
                log.warning("turnstile: egress check failed: %s", str(e)[:120])

            # -------- load target page
            await page.goto(pageurl, wait_until="domcontentloaded",
                            timeout=NAV_TIMEOUT_MS)

            # -------- find turnstile iframe / widget
            # Try a few known selectors and wait for the widget to render
            deadline = time.time() + SOLVE_TIMEOUT_S
            token: Optional[str] = None
            clicked = False

            while time.time() < deadline:
                # Try to read the token from the hidden input the widget fills
                try:
                    token = await page.evaluate(
                        """() => {
                            const el = document.querySelector(
                              'input[name="cf-turnstile-response"]');
                            return el ? el.value : null;
                        }"""
                    )
                except Exception:
                    token = None

                if token:
                    log.info("turnstile: token acquired len=%d", len(token))
                    return token

                # Click the widget once (patchright often passes with one click)
                if not clicked:
                    for sel in (
                        "iframe[src*='challenges.cloudflare.com']",
                        "div.cf-turnstile",
                        "[data-sitekey]",
                    ):
                        try:
                            el = await page.query_selector(sel)
                            if el:
                                box = await el.bounding_box()
                                if box:
                                    await page.mouse.click(
                                        box["x"] + box["width"] / 2,
                                        box["y"] + box["height"] / 2,
                                    )
                                    clicked = True
                                    log.info("turnstile: widget clicked")
                                    break
                        except Exception:
                            continue

                await asyncio.sleep(1.0)

            log.warning("turnstile: timeout, no token")
            return None
        finally:
            try:
                await browser.close()
            except Exception:
                pass


# ---------------------------------------------------------------- recaptcha
async def _solve_recaptcha(sitekey: str, pageurl: str,
                           proxy: Optional[str]) -> Optional[str]:
    """Minimal reCAPTCHA v2 anchor-click flow. Extend if you need v3."""
    async with async_playwright() as pw:
        browser = await _launch_browser(pw, proxy)
        try:
            context = await browser.new_context(locale="en-US")
            await context.add_init_script(STEALTH_JS)
            page = await context.new_page()
            await page.goto(pageurl, wait_until="domcontentloaded",
                            timeout=NAV_TIMEOUT_MS)

            deadline = time.time() + SOLVE_TIMEOUT_S
            while time.time() < deadline:
                try:
                    tok = await page.evaluate(
                        """() => {
                            const el = document.querySelector(
                              'textarea[name="g-recaptcha-response"]');
                            return el ? el.value : null;
                        }"""
                    )
                except Exception:
                    tok = None
                if tok:
                    return tok
                try:
                    frame = page.frame_locator(
                        "iframe[src*='recaptcha/api2/anchor']")
                    await frame.locator("#recaptcha-anchor").click(timeout=2000)
                except Exception:
                    pass
                await asyncio.sleep(1.0)
            return None
        finally:
            try:
                await browser.close()
            except Exception:
                pass


# ---------------------------------------------------------------- fastapi
app = FastAPI(title="Turnstile Solver")


def _check_key(request: Request) -> None:
    if not API_KEY:
        return
    if request.headers.get("x-api-key") != API_KEY:
        raise HTTPException(status_code=401, detail="bad api key")


class SolveReq(BaseModel):
    sitekey: str
    pageurl: str
    proxy: Optional[str] = None
    kind: str = "turnstile"   # "turnstile" | "recaptcha"


class DebugReq(BaseModel):
    sitekey: str
    pageurl: str
    proxy: Optional[str] = None
    kind: str = "turnstile"


@app.get("/health")
async def health():
    return {"ok": True, "tasks": len(TASKS)}


@app.post("/v1/solve/turnstile")
async def solve_turnstile(req: SolveReq, request: Request):
    _check_key(request)

    proxy_used = req.proxy
    tid = await _task_create(proxy_used)
    log.info("task=%s sitekey=%s proxy=%s",
             tid[:8], req.sitekey[:16], "yes" if req.proxy else "no")

    asyncio.create_task(_run_task(
        tid, req.sitekey, req.pageurl, req.proxy, req.kind))
    return {"id": tid}


@app.post("/v1/solve/recaptcha")
async def solve_recaptcha(req: SolveReq, request: Request):
    _check_key(request)
    tid = await _task_create(req.proxy)
    asyncio.create_task(_run_task(
        tid, req.sitekey, req.pageurl, req.proxy, "recaptcha"))
    return {"id": tid}


async def _run_task(tid: str, sitekey: str, pageurl: str,
                    proxy: Optional[str], kind: str) -> None:
    try:
        if kind == "recaptcha":
            tok = await _solve_recaptcha(sitekey, pageurl, proxy)
        else:
            tok = await _solve_turnstile(sitekey, pageurl, proxy)

        if tok:
            await _task_update(tid, "done", result=tok)
            log.info("task=%s done len=%d", tid[:8], len(tok))
        else:
            await _task_update(
                tid, "failed",
                error=f"navigation_failed or timeout on {pageurl}")
            log.warning("task=%s failed", tid[:8])
    except Exception as e:
        await _task_update(tid, "failed", error=str(e)[:200])
        log.exception("task=%s exception", tid[:8])


@app.get("/v1/tasks/{tid}/wait")
async def task_wait(tid: str, timeout: int = 60):
    """Long-poll until the task finishes or timeout elapses."""
    deadline = time.time() + max(1, min(timeout, 180))
    while time.time() < deadline:
        async with TASKS_LOCK:
            t = TASKS.get(tid)
        if not t:
            raise HTTPException(status_code=404, detail="unknown task")
        if t.status in ("done", "failed"):
            return {
                "status": t.status,
                "result": t.result,
                "error": t.error,
            }
        await asyncio.sleep(0.5)
    return {"status": "pending", "result": None, "error": None}


@app.post("/v1/debug/turnstile")
async def debug_turnstile(req: DebugReq, request: Request):
    """Synchronous debug: returns screenshot + iframe list. No token wait."""
    _check_key(request)
    async with async_playwright() as pw:
        browser = await _launch_browser(pw, req.proxy)
        try:
            page = await (await browser.new_context()).new_page()
            await page.add_init_script(STEALTH_JS)

            # Verify egress IP
            await page.goto("https://api.ipify.org",
                            wait_until="domcontentloaded", timeout=15000)
            egress = (await page.inner_text("body")).strip()

            # Load target
            try:
                resp = await page.goto(req.pageurl,
                                       wait_until="domcontentloaded",
                                       timeout=NAV_TIMEOUT_MS)
                status = resp.status if resp else None
            except Exception as e:
                return {"ok": False, "egress_ip": egress,
                        "error": str(e)[:200]}

            iframes = await page.eval_on_selector_all(
                "iframe",
                "els => els.map(e => e.src).filter(s => !!s)",
            )
            await page.screenshot(path="/tmp/debug.png", full_page=True)

            return {
                "ok": True,
                "egress_ip": egress,
                "nav_status": status,
                "url": page.url,
                "iframes": iframes[:20],
                "screenshot": "/tmp/debug.png",
            }
        finally:
            try:
                await browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
