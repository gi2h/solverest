"""
Turnstile solver — v9 (Dynamic & Human-like)
- Simulasi scroll & pergerakan mouse sebelum klik.
- Deteksi Cloudflare Interactive Challenge (gambar).
- Human-like delays & fingerprinting.
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

PAGE_CLOSE_TIMEOUT = 5.0
CONTEXT_CLOSE_TIMEOUT = 5.0
BROWSER_CLOSE_TIMEOUT = 10.0

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
        "--window-size=1366,800",
    ]

def _start_xvfb():
    from pyvirtualdisplay import Display
    display = Display(visible=0, size=(1366, 800))
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
            if not isinstance(c, dict) or "name" not in c: continue
            cc = dict(c)
            cc.setdefault("value", "")
            if "url" not in cc and "domain" not in cc:
                cc["url"] = base_url
            out.append(cc)
        return out
    out = []
    for part in str(cookies).split(";"):
        part = part.strip()
        if not part or "=" not in part: continue
        name, value = part.split("=", 1)
        out.append({"name": name.strip(), "value": value.strip(), "url": base_url})
    return out

async def _new_context(browser, cookies_list=None):
    ctx = await browser.new_context(
        viewport={"width": 1366, "height": 800},
        user_agent=USER_AGENT,
        locale="en-US",
        timezone_id="America/New_York",
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
        },
    )
    if cookies_list:
        try:
            await ctx.add_cookies(cookies_list)
            log.info("turnstile: added %d cookies", len(cookies_list))
        except Exception as e:
            log.warning("add_cookies failed: %s", e)
    return ctx

async def _safe_cleanup(page, context, browser):
    if page:
        try: await asyncio.wait_for(page.close(run_before_unload=False), timeout=PAGE_CLOSE_TIMEOUT)
        except Exception: pass
    if context:
        try: await asyncio.wait_for(context.close(), timeout=CONTEXT_CLOSE_TIMEOUT)
        except Exception: pass
    if browser:
        try: await asyncio.wait_for(browser.close(), timeout=BROWSER_CLOSE_TIMEOUT)
        except Exception: pass

async def _read_token(page) -> Optional[str]:
    try:
        return await page.evaluate(
            """() => {
                const el = document.querySelector('[name="cf-turnstile-response"]');
                return (el && el.value && el.value.length > 20) ? el.value : null;
            }"""
        )
    except Exception:
        return None

async def _is_interactive_challenge(page) -> bool:
    """Deteksi apakah Cloudflare memunculkan tantangan gambar (Image Grid)."""
    try:
        return await page.evaluate("""() => {
            const body = document.body ? document.body.innerText.toLowerCase() : "";
            if (body.includes("select all images") || body.includes("verify you are human")) return true;
            const iframes = Array.from(document.querySelectorAll('iframe[src*="challenges.cloudflare.com"]'));
            for (let f of iframes) {
                const rect = f.getBoundingClientRect();
                // Tantangan gambar biasanya memiliki ukuran > 250x250
                if (rect.width > 250 && rect.height > 250) return true;
            }
            return false;
        }""")
    except Exception:
        return False

async def _simulate_human(page):
    """Bergerak seperti manusia sebelum mengklik checkbox."""
    try:
        # Scroll acak
        await page.mouse.wheel(0, random.randint(100, 300))
        await asyncio.sleep(random.uniform(0.5, 1.2))
        await page.mouse.wheel(0, random.randint(-100, -50))
        await asyncio.sleep(random.uniform(0.3, 0.8))
        
        # Gerakan mouse acak
        for _ in range(3):
            x = random.randint(200, 1000)
            y = random.randint(100, 600)
            await page.mouse.move(x, y, steps=random.randint(5, 15))
            await asyncio.sleep(random.uniform(0.1, 0.3))
    except Exception:
        pass

def _ok(token: str, attempts: int, started: float) -> dict:
    return {"success": True, "token": token, "attempts": attempts, "elapsed_ms": int((time.time() - started) * 1000)}

def _fail(error: str, started: float, attempts: int = 0) -> dict:
    return {"success": False, "error": error, "attempts": attempts, "elapsed_ms": int((time.time() - started) * 1000)}

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
            headless = True

    try:
        launch_opts = {"headless": headless, "args": _launch_args(), "chromium_sandbox": False}
        if proxy:
            launch_opts["proxy"] = {"server": proxy}

        async with async_playwright() as p:
            browser = await p.chromium.launch(**launch_opts)
            context, page = None, None
            try:
                context = await _new_context(browser, cookies_list=parsed_cookies)
                await context.add_init_script(STEALTH_JS)
                page = await context.new_page()

                log.info("turnstile: navigating to %s", pageurl)
                try:
                    await page.goto(pageurl, wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    return _fail(f"navigation_failed: {e}", started)

                await asyncio.sleep(2)
                await _simulate_human(page)

                # Tunggu widget muncul
                widget_appeared = False
                for i in range(15):
                    await asyncio.sleep(1)
                    if await page.query_selector('iframe[src*="challenges.cloudflare.com"], .cf-turnstile'):
                        widget_appeared = True
                        break
                
                if not widget_appeared:
                    return _fail("turnstile_widget_not_found", started)

                # Cek token auto-pass
                token = await _read_token(page)
                if token:
                    return _ok(token, 0, started)

                # Klik widget
                iframe_el = await page.query_selector('iframe[src*="challenges.cloudflare.com"]')
                if iframe_el:
                    await iframe_el.scroll_into_view_if_needed()
                    await asyncio.sleep(0.5)
                    box = await iframe_el.bounding_box()
                    if box:
                        cx = box["x"] + random.uniform(25, 35)
                        cy = box["y"] + (box["height"] / 2) + random.uniform(-3, 3)
                        await page.mouse.move(cx - 50, cy - 40, steps=10)
                        await page.mouse.click(cx, cy)
                        log.info("clicked checkbox at (%.0f, %.0f)", cx, cy)

                # Loop tunggu token & deteksi gambar
                deadline = asyncio.get_event_loop().time() + timeout
                attempts = 0
                while asyncio.get_event_loop().time() < deadline:
                    attempts += 1
                    token = await _read_token(page)
                    if token:
                        return _ok(token, attempts, started)
                    
                    if await _is_interactive_challenge(page):
                        log.warning("Cloudflare Interactive Challenge (Image Grid) detected! IP is flagged.")
                        return _fail("cloudflare_interactive_challenge", started, attempts)

                    if attempts % 10 == 0:
                        log.info("turnstile attempt %d: waiting for token...", attempts)

                    await asyncio.sleep(1)

                return _fail("turnstile_token_timeout", started, attempts)

            finally:
                await _safe_cleanup(page, context, browser)
    except Exception as e:
        return _fail(f"unexpected: {type(e).__name__}: {e}", started)
    finally:
        if display:
            try: display.stop()
            except Exception: pass
