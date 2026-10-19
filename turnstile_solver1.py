"""
Turnstile + reCAPTCHA solver using Patchright + Xvfb.
v3 - Kompatibel dengan app.py dan sig25.py.

Perubahan dari v2:
- Signature menerima `cookies` (string atau list dict) dan `user_agent`.
- Semua return berupa dict: {"success", "token", "error", "attempts", "elapsed_ms"}.
- Exception dikonversi jadi return `success=False` (kecuali ImportError patchright).
- Logika stealth, CF detection, tolerant wait, xvfb tetap UTUH.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import random
import time
from typing import List, Optional, Union
from urllib.parse import urlparse

log = logging.getLogger("solver")
logging.basicConfig(
    level=logging.INFO,
    format="[solver] %(message)s",
)


USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]
USER_AGENT = USER_AGENTS[0]  # kompat backward


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


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────

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
    """
    Terima 'a=1; b=2' atau list dict Playwright.
    Return list dict yang siap dipakai `context.add_cookies()`.
    """
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
        out.append({
            "name": name.strip(),
            "value": value.strip(),
            "url": base_url,
        })
    return out


async def _new_context(browser, cookies_list=None, user_agent=None):
    ctx = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent=user_agent or random.choice(USER_AGENTS),
        locale="en-US",
        timezone_id="America/New_York",
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Linux"',
        },
    )
    if cookies_list:
        try:
            await ctx.add_cookies(cookies_list)
            log.info("turnstile: added %d cookies to context", len(cookies_list))
        except Exception as e:
            log.warning("add_cookies failed: %s", e)
    return ctx


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


async def _page_stats(page) -> dict:
    try:
        return await page.evaluate("""
            () => {
                const iframes = Array.from(document.querySelectorAll('iframe'));
                const title = document.title;
                const url = window.location.href;
                const bodyText = document.body ? document.body.innerText : '';

                const titleLower = title.toLowerCase();
                const hasChallengeForm = !!document.querySelector(
                    '#challenge-form, .challenge-form, #challenge-stage'
                );
                const hasCfWrapper = !!document.querySelector(
                    '#cf-challenge-running, #cf-please-wait'
                );
                const bodySaysJustAMoment =
                    bodyText.toLowerCase().includes('just a moment') ||
                    bodyText.toLowerCase().includes('verifying you are human');

                const isCfChallenge = (
                    titleLower.includes('just a moment') ||
                    bodySaysJustAMoment ||
                    (hasChallengeForm && hasCfWrapper)
                );

                const hasTurnstileDiv = !!document.querySelector(
                    '.cf-turnstile, [data-sitekey], [name="cf-turnstile-response"]'
                );
                const turnstileInput = document.querySelector(
                    '[name="cf-turnstile-response"]'
                );

                return {
                    title: title,
                    url: url,
                    iframeCount: iframes.length,
                    iframeSrcs: iframes.map(f => (f.src || '').slice(0, 150)).slice(0, 5),
                    bodyLen: document.body ? document.body.innerHTML.length : 0,
                    isCfChallenge: isCfChallenge,
                    hasTurnstileDiv: hasTurnstileDiv,
                    hasTurnstileInput: !!turnstileInput,
                    inputValue: turnstileInput ? (turnstileInput.value || '').length : 0,
                };
            }
        """)
    except Exception as e:
        return {"error": str(e)[:200]}


async def _find_turnstile_iframe(page):
    selectors = [
        'iframe[src*="challenges.cloudflare.com"]',
        'iframe[src*="turnstile"]',
        'iframe[title*="Turnstile"]',
        'iframe[title*="Widget containing"]',
        'iframe[title*="cloudflare"]',
        'iframe[id*="cf-chl-widget"]',
        'iframe[src*="cf-chl"]',
    ]
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el:
                box = await el.bounding_box()
                if box and box["width"] > 10 and box["height"] > 10:
                    log.info("found iframe via selector: %s", sel)
                    return el
        except Exception:
            continue
    return None


async def _click_widget(page, iframe_el) -> bool:
    try:
        box = await iframe_el.bounding_box()
        if not box:
            return False
        cx = box["x"] + 30
        cy = box["y"] + (box["height"] / 2)
        await page.mouse.move(cx - 50, cy - 40, steps=8)
        await asyncio.sleep(0.3)
        await page.mouse.move(cx, cy, steps=12)
        await asyncio.sleep(0.2)
        await page.mouse.click(cx, cy)
        await asyncio.sleep(0.5)
        log.info("click: done at (%.0f, %.0f)", cx, cy)
        return True
    except Exception as e:
        log.info("click failed: %s", e)
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


# ─────────────────────────────────────────────────────────────────
# solve_turnstile — signature selaras dengan app.py & sig25.py
# ─────────────────────────────────────────────────────────────────

async def solve_turnstile(
    sitekey: str = "",
    pageurl: str = "",
    timeout: int = 90,
    proxy: Optional[str] = None,
    headless: Optional[bool] = None,
    cookies: Optional[Union[str, List[dict]]] = None,
    user_agent: Optional[str] = None,
) -> dict:
    """
    Return selalu dict:
        {"success": True,  "token": "...", "attempts": N, "elapsed_ms": M}
        {"success": False, "error": "...", "attempts": N, "elapsed_ms": M}
    """
    started = time.time()

    if not pageurl:
        return _fail("pageurl_required", started)

    if headless is None:
        headless = os.environ.get("HEADLESS", "false").lower() != "true"

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
        log.info(
            "turnstile: launching (headless=%s, cookies=%d, ua=%s)",
            headless,
            len(parsed_cookies),
            (user_agent or USER_AGENT)[:48],
        )
        launch_opts = {
            "headless": headless,
            "args": _launch_args(),
            "chromium_sandbox": False,
        }
        if proxy:
            launch_opts["proxy"] = {"server": proxy}

        async with async_playwright() as p:
            browser = await p.chromium.launch(**launch_opts)
            try:
                context = await _new_context(
                    browser,
                    cookies_list=parsed_cookies,
                    user_agent=user_agent,
                )
                await context.add_init_script(STEALTH_JS)
                page = await context.new_page()

                log.info("turnstile: navigating to %s", pageurl)
                try:
                    await page.goto(
                        pageurl,
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                except Exception as e:
                    return _fail(f"navigation_failed: {e}", started)

                log.info("turnstile: page loaded url=%s", page.url)

                await asyncio.sleep(3)

                stats = await _page_stats(page)
                log.info(
                    "turnstile: stats title=%r iframes=%d cf=%s turnstileDiv=%s",
                    stats.get("title", "")[:60],
                    stats.get("iframeCount", 0),
                    stats.get("isCfChallenge", False),
                    stats.get("hasTurnstileDiv", False),
                )

                # Early token (auto-solve <3s)
                token = await _read_token(page, "cf-turnstile-response")
                if token:
                    log.info("turnstile: token present early len=%d", len(token))
                    return _ok(token, 0, started)

                # CF interstitial handling
                if stats.get("isCfChallenge"):
                    log.info("turnstile: real CF challenge detected, waiting up to 20s")
                    cf_deadline = asyncio.get_event_loop().time() + 20
                    while asyncio.get_event_loop().time() < cf_deadline:
                        await asyncio.sleep(2)
                        new_stats = await _page_stats(page)
                        if not new_stats.get("isCfChallenge"):
                            log.info("turnstile: CF challenge cleared")
                            break
                        token = await _read_token(page, "cf-turnstile-response")
                        if token:
                            return _ok(token, 0, started)

                # Tolerant wait loop
                log.info("turnstile: waiting for token (timeout=%ds)", timeout)
                deadline = asyncio.get_event_loop().time() + timeout
                iframe_el = None
                clicked = False
                attempts = 0
                last_log = 0

                while asyncio.get_event_loop().time() < deadline:
                    attempts += 1

                    token = await _read_token(page, "cf-turnstile-response")
                    if token:
                        log.info(
                            "turnstile: token acquired len=%d at attempt %d",
                            len(token),
                            attempts,
                        )
                        return _ok(token, attempts, started)

                    if not iframe_el:
                        iframe_el = await _find_turnstile_iframe(page)
                        if iframe_el and not clicked:
                            clicked = await _click_widget(page, iframe_el)

                    if attempts - last_log >= 10:
                        last_log = attempts
                        s = await _page_stats(page)
                        log.info(
                            "turnstile attempt %d: iframes=%d cf=%s input_len=%d",
                            attempts,
                            s.get("iframeCount", 0),
                            s.get("isCfChallenge", False),
                            s.get("inputValue", 0),
                        )

                    await asyncio.sleep(1)

                stats = await _page_stats(page)
                return _fail(
                    "turnstile_token_timeout "
                    f"(url={page.url}, iframes={stats.get('iframeCount', 0)}, "
                    f"cf={stats.get('isCfChallenge', False)})",
                    started,
                    attempts,
                )

            finally:
                try:
                    await browser.close()
                except Exception:
                    pass

    except Exception as e:
        log.exception("turnstile: unexpected error")
        return _fail(f"unexpected: {type(e).__name__}: {e}", started)
    finally:
        if display:
            try:
                display.stop()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────
# Optional: reCAPTCHA & CF cookie (dipertahankan dari v2)
# ─────────────────────────────────────────────────────────────────

async def solve_recaptcha(
    sitekey: str,
    pageurl: str,
    timeout: int = 90,
    proxy: Optional[str] = None,
    headless: Optional[bool] = None,
) -> dict:
    started = time.time()
    if headless is None:
        headless = os.environ.get("HEADLESS", "false").lower() != "true"
    try:
        from patchright.async_api import async_playwright
    except ImportError as e:
        return _fail(f"patchright_not_installed: {e}", started)

    display = None
    if not headless:
        try:
            display = _start_xvfb()
        except Exception as e:
            log.warning("xvfb failed: %s", e)
            headless = True

    try:
        launch_opts = {
            "headless": headless,
            "args": _launch_args(),
            "chromium_sandbox": False,
        }
        if proxy:
            launch_opts["proxy"] = {"server": proxy}

        async with async_playwright() as p:
            browser = await p.chromium.launch(**launch_opts)
            try:
                context = await _new_context(browser)
                await context.add_init_script(STEALTH_JS)
                page = await context.new_page()

                await page.goto(pageurl, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(3)

                iframe_sel = (
                    'iframe[src*="google.com/recaptcha"], '
                    'iframe[title*="recaptcha"]'
                )
                deadline = asyncio.get_event_loop().time() + timeout
                clicked = False
                iframe_el = None
                attempts = 0

                while asyncio.get_event_loop().time() < deadline:
                    attempts += 1
                    token = await _read_token(page, "g-recaptcha-response")
                    if token:
                        return _ok(token, attempts, started)
                    if not iframe_el:
                        iframe_el = await page.query_selector(iframe_sel)
                        if iframe_el and not clicked:
                            clicked = await _click_widget(page, iframe_el)
                    await asyncio.sleep(1)

                return _fail("recaptcha_token_timeout", started, attempts)
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass
    except Exception as e:
        return _fail(f"unexpected: {type(e).__name__}: {e}", started)
    finally:
        if display:
            try:
                display.stop()
            except Exception:
                pass


async def get_cloudflare_cookie(
    url: str,
    timeout: int = 60,
    proxy: Optional[str] = None,
    headless: Optional[bool] = None,
) -> dict:
    started = time.time()
    if headless is None:
        headless = os.environ.get("HEADLESS", "false").lower() != "true"
    try:
        from patchright.async_api import async_playwright
    except ImportError as e:
        return _fail(f"patchright_not_installed: {e}", started)

    display = None
    if not headless:
        try:
            display = _start_xvfb()
        except Exception as e:
            log.warning("xvfb failed: %s", e)
            headless = True

    try:
        launch_opts = {
            "headless": headless,
            "args": _launch_args(),
            "chromium_sandbox": False,
        }
        if proxy:
            launch_opts["proxy"] = {"server": proxy}

        async with async_playwright() as p:
            browser = await p.chromium.launch(**launch_opts)
            try:
                context = await _new_context(browser)
                await context.add_init_script(STEALTH_JS)
                page = await context.new_page()

                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(3)

                deadline = asyncio.get_event_loop().time() + timeout
                while asyncio.get_event_loop().time() < deadline:
                    cookies = await context.cookies()
                    if any(c["name"] == "cf_clearance" for c in cookies):
                        break
                    await asyncio.sleep(1)

                cookies = await context.cookies()
                return {
                    "success": True,
                    "cookies": {c["name"]: c["value"] for c in cookies},
                    "user_agent": USER_AGENT,
                    "final_url": page.url,
                    "html": await page.content(),
                    "elapsed_ms": int((time.time() - started) * 1000),
                }
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass
    except Exception as e:
        return _fail(f"unexpected: {type(e).__name__}: {e}", started)
    finally:
        if display:
            try:
                display.stop()
            except Exception:
                pass
