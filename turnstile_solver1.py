"""
Turnstile + reCAPTCHA solver using Patchright + Xvfb.
v2.7 - WAF detection, wait for interstitial to clear, extract cf_clearance.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
from typing import Optional

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


async def _new_context(browser):
    return await browser.new_context(
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


async def _safe_cleanup(page, context, browser):
    if page is not None:
        try: await asyncio.wait_for(page.close(run_before_unload=False), timeout=PAGE_CLOSE_TIMEOUT)
        except Exception: pass
    if context is not None:
        try: await asyncio.wait_for(context.close(), timeout=CONTEXT_CLOSE_TIMEOUT)
        except Exception: pass
    if browser is not None:
        try: await asyncio.wait_for(browser.close(), timeout=BROWSER_CLOSE_TIMEOUT)
        except Exception: pass


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
    """Mengecek apakah halaman masih menampilkan WAF Cloudflare (Just a moment)."""
    try:
        return await page.evaluate("""
            () => {
                const title = document.title || "";
                const body = document.body ? document.body.innerText : "";
                if (title.includes("Just a moment") || body.includes("Just a moment") || body.includes("Verifying you are human")) {
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


async def solve_turnstile(
    sitekey: str,
    pageurl: str,
    timeout: int = 90,
    proxy: Optional[str] = None,
    headless: bool = False,
) -> dict:
    try:
        from patchright.async_api import async_playwright
    except ImportError as e:
        raise RuntimeError(f"patchright_not_installed: {e}")

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
                context = await _new_context(browser)
                await context.add_init_script(STEALTH_JS)
                page = await context.new_page()

                log.info("turnstile: navigating to %s (proxy=%s)", pageurl, bool(proxy))
                try:
                    await page.goto(pageurl, wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    raise RuntimeError(f"navigation_failed: {e}")

                await asyncio.sleep(3)

                # === WAF CHECK ===
                waf_attempts = 0
                while await _is_waf_active(page):
                    waf_attempts += 1
                    if waf_attempts > 10:
                        raise RuntimeError("waf_bypass_timeout (Cloudflare interstitial stuck)")
                    log.info("turnstile: WAF 'Just a moment' detected, waiting... (%d/10)", waf_attempts)
                    await asyncio.sleep(4)

                log.info("turnstile: WAF cleared or not present.")

                # === TURNSTILE TOKEN ===
                token = await _read_token(page, "cf-turnstile-response")
                if token:
                    cookies = await context.cookies()
                    cookie_dict = {c["name"]: c["value"] for c in cookies}
                    log.info("turnstile: token early len=%d, cookies=%s", len(token), list(cookie_dict.keys()))
                    return {"token": token, "cookies": cookie_dict, "user_agent": USER_AGENT}

                deadline = asyncio.get_event_loop().time() + timeout
                while asyncio.get_event_loop().time() < deadline:
                    token = await _read_token(page, "cf-turnstile-response")
                    if token:
                        cookies = await context.cookies()
                        cookie_dict = {c["name"]: c["value"] for c in cookies}
                        log.info("turnstile: token acquired len=%d, cookies=%s", len(token), list(cookie_dict.keys()))
                        return {"token": token, "cookies": cookie_dict, "user_agent": USER_AGENT}
                    
                    # Cek WAF lagi barangkali muncul di tengah jalan
                    if await _is_waf_active(page):
                        log.info("turnstile: WAF appeared while waiting token...")
                        await asyncio.sleep(3)
                        continue

                    await asyncio.sleep(1)

                raise RuntimeError("turnstile_token_timeout")

            finally:
                await _safe_cleanup(page, context, browser)
    finally:
        if display:
            try: display.stop()
            except Exception: pass
