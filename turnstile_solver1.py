"""
Turnstile + reCAPTCHA solver using Patchright + Xvfb.
v8 - Fixed signature, return format, and WAF/Turnstile separation.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from typing import List, Optional, Union
from urllib.parse import urlparse

log = logging.getLogger("solver")
logging.basicConfig(level=logging.INFO, format="[solver] %(message)s")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5].map(() => ({
        name: 'Chrome PDF Plugin',
        filename: 'internal-pdf-viewer',
    })),
});
window.chrome = { runtime: {}, loadTimes: () => {}, csi: () => {} };
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters)
);
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(p) {
    if (p === 37445) return 'Intel Inc.';
    if (p === 37446) return 'Intel Iris OpenGL Engine';
    return getParameter.call(this, p);
};
"""

PAGE_CLOSE_TIMEOUT = float(os.getenv("PAGE_CLOSE_TIMEOUT", "5"))
CONTEXT_CLOSE_TIMEOUT = float(os.getenv("CONTEXT_CLOSE_TIMEOUT", "5"))
BROWSER_CLOSE_TIMEOUT = float(os.getenv("BROWSER_CLOSE_TIMEOUT", "10"))

def _launch_args() -> list:
    return [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-features=IsolateOrigins,site-per-process",
        "--window-size=1280,800",
        "--start-maximized",
    ]

def _start_xvfb():
    from pyvirtualdisplay import Display
    display = Display(visible=0, size=(1280, 800))
    display.start()
    log.info("xvfb started DISPLAY=%s", os.environ.get("DISPLAY"))
    return display

def _parse_cookies(cookies, pageurl: str) -> List[dict]:
    if not cookies:
        return []
    parsed = urlparse(pageurl)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    if isinstance(cookies, list):
        out = []
        for c in cookies:
            if not isinstance(c, dict) or "name" not in c:
                continue
            cc = dict(c)
            cc.setdefault("value", "")
            if "url" not in cc and "domain" not in cc:
                cc["url"] = base_url
            out.append(cc)
        return out
    out = []
    for part in str(cookies).split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        out.append({"name": name.strip(), "value": value.strip(), "url": base_url})
    return out

async def _new_context(browser, cookies_list=None):
    ctx = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent=USER_AGENT,
        locale="en-US",
        timezone_id="America/New_York",
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
        },
    )
    if cookies_list:
        try:
            await ctx.add_cookies(cookies_list)
            log.info("turnstile: added %d cookies to context", len(cookies_list))
        except Exception as e:
            log.warning("add_cookies failed: %s", e)
    return ctx

async def _safe_cleanup(page, context, browser):
    if page is not None:
        try:
            await asyncio.wait_for(page.close(run_before_unload=False), timeout=PAGE_CLOSE_TIMEOUT)
        except Exception:
            pass
    if context is not None:
        try:
            await asyncio.wait_for(context.close(), timeout=CONTEXT_CLOSE_TIMEOUT)
        except Exception:
            pass
    if browser is not None:
        try:
            await asyncio.wait_for(browser.close(), timeout=BROWSER_CLOSE_TIMEOUT)
        except Exception:
            pass

async def _read_token(page, input_name: str) -> Optional[str]:
    try:
        return await page.evaluate(
            """(name) => {
                const el = document.querySelector('[name="' + name + '"]');
                return (el && el.value && el.value.length > 20) ? el.value : null;
            }""",
            input_name,
        )
    except Exception:
        return None

async def _is_waf_active(page) -> bool:
    """
    Cek apakah ini halaman blokir Cloudflare (WAF).
    JANGAN cek iframe challenges.cloudflare.com di sini, karena itu adalah widget Turnstile.
    """
    try:
        return await page.evaluate("""
            () => {
                const title = document.title || "";
                const body = document.body ? document.body.innerText : "";
                if (title.includes("Just a moment") || body.includes("Verifying you are human")) {
                    return true;
                }
                if (document.querySelector("#challenge-form") || document.querySelector("#challenge-stage")) {
                    return true;
                }
                return false;
            }
        """)
    except Exception:
        return False

async def _click_waf_checkbox(page) -> bool:
    try:
        iframe_el = await page.query_selector('iframe[src*="challenges.cloudflare.com"]')
        if iframe_el:
            box = await iframe_el.bounding_box()
            if box:
                # Klik di kiri tengah (posisi checkbox)
                cx = box["x"] + 30
                cy = box["y"] + (box["height"] / 2)
                await page.mouse.move(cx - 50, cy - 50, steps=10)
                await asyncio.sleep(0.3)
                await page.mouse.click(cx, cy)
                log.info("WAF: clicked checkbox at (%.0f, %.0f)", cx, cy)
                return True
    except Exception:
        pass
    return False

def _ok(token: str, attempts: int, started: float) -> dict:
    return {
        "success": True,
        "token": token,
        "attempts": attempts,
        "elapsed_ms": int((time.time() - started) * 1000),
    }

def _fail(error: str, started: float, attempts: int = 0) -> dict:
    return {
        "success": False,
        "error": error,
        "attempts": attempts,
        "elapsed_ms": int((time.time() - started) * 1000),
    }

async def solve_turnstile(
    sitekey: str = "",
    pageurl: str = "",
    timeout: int = 90,
    proxy: Optional[str] = None,
    headless: Optional[bool] = None,
    cookies: Optional[Union[str, List[dict]]] = None,
    user_agent: Optional[str] = None,
) -> dict:
    started = time.time()
    if not pageurl:
        return _fail("pageurl_required", started)
        
    if headless is None:
        headless = os.environ.get("HEADLESS", "false").lower() == "true"

    try:
        from patchright.async_api import async_playwright
    except ImportError as e:
        return _fail(f"patchright_not_installed: {e}", started)

    parsed_cookies = _parse_cookies(cookies, pageurl)
    display = None
    if not headless:
        try:
            display = _start_xvfb()
        except Exception as e:
            log.warning("xvfb failed (%s), falling back to headless", e)
            headless = True

    try:
        launch_opts = {"headless": headless, "args": _launch_args(), "chromium_sandbox": False}
        if proxy:
            launch_opts["proxy"] = {"server": proxy}

        async with async_playwright() as p:
            browser = await p.chromium.launch(**launch_opts)
            context = None
            page = None
            try:
                context = await _new_context(browser, cookies_list=parsed_cookies)
                await context.add_init_script(STEALTH_JS)
                page = await context.new_page()

                log.info("turnstile: navigating to %s (proxy=%s)", pageurl, bool(proxy))
                try:
                    await page.goto(pageurl, wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    return _fail(f"navigation_failed: {e}", started)

                await asyncio.sleep(3)

                # 1. WAF CHECK & CLICK
                waf_attempts = 0
                while await _is_waf_active(page):
                    waf_attempts += 1
                    if waf_attempts > 8:
                        return _fail("waf_bypass_timeout (Cloudflare blocked datacenter IP)", started)
                    
                    log.info("turnstile: WAF detected, trying to interact... (%d/8)", waf_attempts)
                    await _click_waf_checkbox(page)
                    await asyncio.sleep(4)

                log.info("turnstile: WAF cleared or not present.")

                # 2. TOKEN LOOP
                deadline = asyncio.get_event_loop().time() + timeout
                attempts = 0
                clicked_widget = False
                
                while asyncio.get_event_loop().time() < deadline:
                    attempts += 1
                    token = await _read_token(page, "cf-turnstile-response")
                    if token:
                        return _ok(token, attempts, started)
                        
                    # Jika ini widget Turnstile (bukan WAF) dan belum pernah diklik
                    if not clicked_widget and attempts > 3:
                        iframe_el = await page.query_selector('iframe[src*="challenges.cloudflare.com"]')
                        if iframe_el:
                            box = await iframe_el.bounding_box()
                            # Pastikan ini widget kecil, bukan halaman challenge fullscreen
                            if box and box["width"] < 400 and box["height"] < 200:
                                cx = box["x"] + 30
                                cy = box["y"] + (box["height"] / 2)
                                await page.mouse.move(cx - 50, cy - 50, steps=10)
                                await asyncio.sleep(0.3)
                                await page.mouse.click(cx, cy)
                                clicked_widget = True
                                log.info("turnstile: clicked widget checkbox (once)")
                                
                    await asyncio.sleep(1)

                return _fail("turnstile_token_timeout", started, attempts)

            finally:
                await _safe_cleanup(page, context, browser)
    except Exception as e:
        log.exception("turnstile: unexpected error")
        return _fail(f"unexpected: {type(e).__name__}: {e}", started)
    finally:
        if display:
            try: display.stop()
            except Exception: pass


# ─────────────────────────────────────────────────────────────
# reCAPTCHA & get_cloudflare_cookie (Opsional, dibiarkan aman)
# ─────────────────────────────────────────────────────────────
async def solve_recaptcha(
    sitekey: str,
    pageurl: str,
    timeout: int = 90,
    proxy: Optional[str] = None,
    headless: Optional[bool] = None,
) -> dict:
    started = time.time()
    if headless is None:
        headless = os.environ.get("HEADLESS", "false").lower() == "true"

    try:
        from patchright.async_api import async_playwright
    except ImportError as e:
        return _fail(f"patchright_not_installed: {e}", started)

    display = None
    if not headless:
        try:
            display = _start_xvfb()
        except Exception as e:
            headless = True

    try:
        launch_opts = {"headless": headless, "args": _launch_args(), "chromium_sandbox": False}
        if proxy:
            launch_opts["proxy"] = {"server": proxy}

        async with async_playwright() as p:
            browser = await p.chromium.launch(**launch_opts)
            context = None
            page = None
            try:
                context = await _new_context(browser)
                await context.add_init_script(STEALTH_JS)
                page = await context.new_page()

                await page.goto(pageurl, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(3)

                deadline = asyncio.get_event_loop().time() + timeout
                attempts = 0
                while asyncio.get_event_loop().time() < deadline:
                    attempts += 1
                    token = await _read_token(page, "g-recaptcha-response")
                    if token:
                        return _ok(token, attempts, started)
                    await asyncio.sleep(1)

                return _fail("recaptcha_token_timeout", started, attempts)

            finally:
                await _safe_cleanup(page, context, browser)
    except Exception as e:
        return _fail(f"unexpected: {type(e).__name__}: {e}", started)
    finally:
        if display:
            try: display.stop()
            except Exception: pass
