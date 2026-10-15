"""
Turnstile + reCAPTCHA solver using Patchright with Xvfb (non-headless).

Runs Chromium in a virtual display via pyvirtualdisplay so Cloudflare
cannot detect the "headless" fingerprint.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
from typing import Optional

log = logging.getLogger("solver")
logging.basicConfig(
    level=logging.INFO,
    format="[solver] %(message)s",
)


USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


# JavaScript injected BEFORE page load to hide automation
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
        "--window-position=0,0",
        "--start-maximized",
    ]


def _start_xvfb():
    """Start virtual display. Returns display object (must be kept alive)."""
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
            "Sec-Ch-Ua-Platform": '"Linux"',
        },
    )


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


async def _list_iframes(page) -> list:
    try:
        return await page.evaluate("""
            () => Array.from(document.querySelectorAll('iframe')).map(f => ({
                src: f.src || '',
                title: f.title || '',
                id: f.id || '',
                class: f.className || '',
                visible: f.offsetWidth > 0 && f.offsetHeight > 0,
                w: f.offsetWidth,
                h: f.offsetHeight,
            }))
        """)
    except Exception:
        return []


async def _find_turnstile_iframe(page):
    """Try multiple selectors to find Turnstile iframe."""
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

        log.info(
            "click: iframe at x=%.0f y=%.0f w=%.0f h=%.0f",
            box["x"], box["y"], box["width"], box["height"],
        )

        # Turnstile checkbox: left ~30px, center vertical
        cx = box["x"] + 30
        cy = box["y"] + (box["height"] / 2)

        # Human-like mouse approach
        await page.mouse.move(cx - 50, cy - 40, steps=8)
        await asyncio.sleep(0.3)
        await page.mouse.move(cx - 20, cy - 10, steps=6)
        await asyncio.sleep(0.2)
        await page.mouse.move(cx, cy, steps=8)
        await asyncio.sleep(0.2)
        await page.mouse.click(cx, cy)
        await asyncio.sleep(0.5)

        log.info("click: done at (%.0f, %.0f)", cx, cy)
        return True
    except Exception as e:
        log.info("click failed: %s", e)
        return False


async def _dump_debug(page) -> dict:
    """Collect debug info: screenshot + iframe list + URL."""
    info = {
        "url": page.url,
        "title": "",
        "iframe_count": 0,
        "iframes": [],
        "screenshot_b64": "",
    }
    try:
        info["title"] = await page.title()
    except Exception:
        pass
    try:
        iframes = await _list_iframes(page)
        info["iframes"] = iframes
        info["iframe_count"] = len(iframes)
    except Exception:
        pass
    try:
        shot = await page.screenshot(full_page=False)
        info["screenshot_b64"] = base64.b64encode(shot).decode()[:200000]
    except Exception:
        pass
    return info


async def solve_turnstile(
    sitekey: str,
    pageurl: str,
    timeout: int = 90,
    proxy: Optional[str] = None,
    headless: bool = False,
) -> dict:
    """
    Solve Cloudflare Turnstile widget.
    Returns: {"token": str, "debug": {...}}
    Raises RuntimeError on failure.
    """
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
        log.info("turnstile: launching (headless=%s)", headless)
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

                # Capture console messages for debug
                console_logs = []
                page.on(
                    "console",
                    lambda msg: console_logs.append(
                        f"{msg.type}: {msg.text[:200]}"
                    ),
                )

                log.info("turnstile: navigating to %s", pageurl)
                try:
                    await page.goto(
                        pageurl,
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                except Exception as e:
                    raise RuntimeError(f"navigation_failed: {e}")

                log.info("turnstile: page loaded url=%s", page.url)
                await asyncio.sleep(2)  # let widget JS boot

                # Check if token already present (auto-solve)
                token = await _read_token(page, "cf-turnstile-response")
                if token:
                    log.info("token present immediately len=%d", len(token))
                    return {"token": token, "debug": {"early": True}}

                # Wait for Turnstile iframe (multiple selectors)
                log.info("waiting for turnstile iframe...")
                deadline = asyncio.get_event_loop().time() + timeout
                iframe_el = None
                attempts = 0

                while asyncio.get_event_loop().time() < deadline:
                    attempts += 1
                    iframe_el = await _find_turnstile_iframe(page)
                    if iframe_el:
                        break

                    # Try token check every loop
                    token = await _read_token(page, "cf-turnstile-response")
                    if token:
                        log.info("token found during iframe wait len=%d", len(token))
                        return {"token": token, "debug": {"attempts": attempts}}

                    if attempts % 5 == 0:
                        iframes = await _list_iframes(page)
                        log.info(
                            "attempt %d: url=%s iframes=%d",
                            attempts, page.url, len(iframes),
                        )
                    await asyncio.sleep(1)

                if not iframe_el:
                    debug = await _dump_debug(page)
                    raise RuntimeError(
                        f"turnstile_iframe_not_found "
                        f"(url={page.url}, iframes={debug['iframe_count']})"
                    )

                log.info("iframe found after %d attempts", attempts)

                # Try clicking checkbox
                clicked = await _click_widget(page, iframe_el)

                # Poll for token
                log.info("waiting for token (timeout=%ds)", timeout)
                deadline = asyncio.get_event_loop().time() + timeout
                poll = 0
                while asyncio.get_event_loop().time() < deadline:
                    poll += 1
                    token = await _read_token(page, "cf-turnstile-response")
                    if token:
                        log.info(
                            "token acquired len=%d (polls=%d)", len(token), poll
                        )
                        return {"token": token, "debug": {"polls": poll}}

                    if clicked and poll % 5 == 0:
                        log.info("re-clicking checkbox (poll %d)", poll)
                        await _click_widget(page, iframe_el)

                    await asyncio.sleep(1)

                debug = await _dump_debug(page)
                raise RuntimeError(
                    f"turnstile_token_timeout "
                    f"(iframe_count={debug['iframe_count']})"
                )

            finally:
                try:
                    await browser.close()
                except Exception:
                    pass

    finally:
        if display:
            try:
                display.stop()
            except Exception:
                pass


async def solve_recaptcha(
    sitekey: str,
    pageurl: str,
    timeout: int = 90,
    proxy: Optional[str] = None,
    headless: bool = False,
) -> dict:
    """Solve reCAPTCHA v2 widget."""
    try:
        from patchright.async_api import async_playwright
    except ImportError as e:
        raise RuntimeError(f"patchright_not_installed: {e}")

    display = None
    if not headless:
        try:
            display = _start_xvfb()
        except Exception as e:
            log.warning("xvfb failed: %s", e)
            headless = True

    try:
        log.info("recaptcha: launching (headless=%s)", headless)
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

                log.info("recaptcha: navigating to %s", pageurl)
                try:
                    await page.goto(
                        pageurl, wait_until="domcontentloaded", timeout=30000
                    )
                except Exception as e:
                    raise RuntimeError(f"navigation_failed: {e}")

                await asyncio.sleep(2)

                # Find recaptcha iframe
                iframe_sel = (
                    'iframe[src*="google.com/recaptcha"], '
                    'iframe[title*="recaptcha"]'
                )
                deadline = asyncio.get_event_loop().time() + timeout
                iframe_el = None
                while asyncio.get_event_loop().time() < deadline:
                    iframe_el = await page.query_selector(iframe_sel)
                    if iframe_el:
                        break
                    token = await _read_token(page, "g-recaptcha-response")
                    if token:
                        return {"token": token, "debug": {}}
                    await asyncio.sleep(1)

                if not iframe_el:
                    raise RuntimeError("recaptcha_iframe_not_found")

                await _click_widget(page, iframe_el)

                deadline = asyncio.get_event_loop().time() + timeout
                while asyncio.get_event_loop().time() < deadline:
                    token = await _read_token(page, "g-recaptcha-response")
                    if token:
                        return {"token": token, "debug": {}}
                    await asyncio.sleep(1)

                raise RuntimeError("recaptcha_token_timeout")

            finally:
                try:
                    await browser.close()
                except Exception:
                    pass
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
    headless: bool = False,
) -> dict:
    """Fetch URL and return cookies + user-agent."""
    try:
        from patchright.async_api import async_playwright
    except ImportError as e:
        raise RuntimeError(f"patchright_not_installed: {e}")

    display = None
    if not headless:
        try:
            display = _start_xvfb()
        except Exception as e:
            log.warning("xvfb failed: %s", e)
            headless = True

    try:
        log.info("cf-cookie: launching (headless=%s)", headless)
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

                log.info("cf-cookie: navigating to %s", url)
                try:
                    await page.goto(
                        url, wait_until="domcontentloaded", timeout=30000
                    )
                except Exception as e:
                    raise RuntimeError(f"navigation_failed: {e}")

                await asyncio.sleep(3)

                deadline = asyncio.get_event_loop().time() + timeout
                while asyncio.get_event_loop().time() < deadline:
                    cookies = await context.cookies()
                    names = {c["name"] for c in cookies}
                    if "cf_clearance" in names:
                        log.info("cf-cookie: got cf_clearance")
                        break
                    await asyncio.sleep(1)

                cookies = await context.cookies()
                cookie_dict = {c["name"]: c["value"] for c in cookies}
                html = await page.content()

                return {
                    "cookies": cookie_dict,
                    "user_agent": USER_AGENT,
                    "html": html,
                    "final_url": page.url,
                }

            finally:
                try:
                    await browser.close()
                except Exception:
                    pass
    finally:
        if display:
            try:
                display.stop()
            except Exception:
                pass
