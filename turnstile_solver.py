"""
Turnstile + reCAPTCHA solver using Patchright + Xvfb.

Debugging-focused: logs HTML sample + console errors when iframe not found.
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


async def _page_stats(page) -> dict:
    """Collect statistics about current page."""
    try:
        return await page.evaluate("""
            () => {
                const iframes = Array.from(document.querySelectorAll('iframe'));
                const scripts = Array.from(document.querySelectorAll('script'));
                const forms = Array.from(document.querySelectorAll('form'));
                const inputs = Array.from(document.querySelectorAll('input'));
                const title = document.title;
                const bodyLen = document.body ? document.body.innerHTML.length : 0;
                const htmlLen = document.documentElement.outerHTML.length;

                // Detect Cloudflare challenge
                const isCfChallenge = !!(
                    document.querySelector('#cf-chl-widget') ||
                    document.querySelector('.cf-challenge') ||
                    document.querySelector('[id*="cf-chl"]') ||
                    (document.body && document.body.innerHTML.includes('cf_chl_opt')) ||
                    (document.title && document.title.toLowerCase().includes('just a moment'))
                );

                // Detect Turnstile div
                const hasTurnstileDiv = !!document.querySelector('.cf-turnstile, [data-sitekey]');

                return {
                    title: title,
                    url: window.location.href,
                    iframeCount: iframes.length,
                    iframeSrcs: iframes.map(f => f.src || '').slice(0, 5),
                    scriptCount: scripts.length,
                    scriptSrcs: scripts.map(s => s.src || '(inline)').slice(0, 10),
                    formCount: forms.length,
                    inputCount: inputs.length,
                    inputNames: inputs.map(i => i.name || '').filter(Boolean).slice(0, 10),
                    bodyLen: bodyLen,
                    htmlLen: htmlLen,
                    isCfChallenge: isCfChallenge,
                    hasTurnstileDiv: hasTurnstileDiv,
                };
            }
        """)
    except Exception as e:
        return {"error": str(e)[:200]}


async def _html_sample(page, max_len: int = 3000) -> str:
    try:
        html = await page.content()
        return html[:max_len]
    except Exception as e:
        return f"<error: {e}>"


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
        log.info(
            "click: iframe at x=%.0f y=%.0f w=%.0f h=%.0f",
            box["x"], box["y"], box["width"], box["height"],
        )
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

    console_errors = []
    page_errors = []

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

                page.on(
                    "console",
                    lambda msg: console_errors.append(
                        f"{msg.type}: {msg.text[:300]}"
                    ) if msg.type in ("error", "warning") else None,
                )
                page.on(
                    "pageerror",
                    lambda err: page_errors.append(str(err)[:300]),
                )
                page.on(
                    "requestfailed",
                    lambda req: console_errors.append(
                        f"REQ_FAILED: {req.url[:100]} - "
                        f"{req.failure if req.failure else 'unknown'}"
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

                # Give JS time to boot + widgets to render
                log.info("turnstile: waiting 5s for JS to boot")
                await asyncio.sleep(5)

                # Log stats after boot
                stats = await _page_stats(page)
                log.info(
                    "turnstile: page stats after boot: "
                    "title=%r url=%s iframes=%d scripts=%d forms=%d "
                    "bodyLen=%d isCfChallenge=%s hasTurnstileDiv=%s",
                    stats.get("title", ""),
                    stats.get("url", ""),
                    stats.get("iframeCount", 0),
                    stats.get("scriptCount", 0),
                    stats.get("formCount", 0),
                    stats.get("bodyLen", 0),
                    stats.get("isCfChallenge", False),
                    stats.get("hasTurnstileDiv", False),
                )
                log.info(
                    "turnstile: iframe srcs: %s",
                    stats.get("iframeSrcs", []),
                )
                log.info(
                    "turnstile: script srcs: %s",
                    stats.get("scriptSrcs", []),
                )
                log.info(
                    "turnstile: input names: %s",
                    stats.get("inputNames", []),
                )

                # If Cloudflare challenge interstitial, wait longer
                if stats.get("isCfChallenge"):
                    log.info("turnstile: Cloudflare challenge detected, waiting up to 30s for redirect")
                    cf_deadline = asyncio.get_event_loop().time() + 30
                    while asyncio.get_event_loop().time() < cf_deadline:
                        await asyncio.sleep(2)
                        new_stats = await _page_stats(page)
                        if not new_stats.get("isCfChallenge"):
                            log.info("turnstile: challenge passed, url=%s", new_stats.get("url", ""))
                            stats = new_stats
                            break
                    else:
                        log.warning("turnstile: CF challenge not cleared after 30s")

                # Check for token first
                token = await _read_token(page, "cf-turnstile-response")
                if token:
                    log.info("turnstile: token present early len=%d", len(token))
                    return {"token": token, "debug": {"early": True, **stats}}

                # Find iframe
                log.info("turnstile: waiting for iframe...")
                deadline = asyncio.get_event_loop().time() + timeout
                iframe_el = None
                attempts = 0
                while asyncio.get_event_loop().time() < deadline:
                    attempts += 1
                    iframe_el = await _find_turnstile_iframe(page)
                    if iframe_el:
                        break

                    token = await _read_token(page, "cf-turnstile-response")
                    if token:
                        return {"token": token, "debug": {"attempts": attempts}}

                    if attempts in (5, 15, 30):
                        s = await _page_stats(page)
                        log.info(
                            "turnstile attempt %d: url=%s iframes=%d "
                            "isCf=%s hasTurnstileDiv=%s",
                            attempts, s.get("url"), s.get("iframeCount"),
                            s.get("isCfChallenge"), s.get("hasTurnstileDiv"),
                        )
                    await asyncio.sleep(1)

                if not iframe_el:
                    # Dump HTML + console errors for diagnosis
                    html_sample = await _html_sample(page, 4000)
                    log.error("turnstile: FAILED - no iframe. HTML sample:")
                    log.error("--- HTML START ---")
                    log.error("%s", html_sample)
                    log.error("--- HTML END ---")
                    log.error(
                        "console errors: %s",
                        console_errors[-10:] if console_errors else "none",
                    )
                    log.error(
                        "page errors: %s",
                        page_errors[-5:] if page_errors else "none",
                    )
                    raise RuntimeError(
                        f"turnstile_iframe_not_found "
                        f"(url={page.url}, "
                        f"iframes={stats.get('iframeCount', 0)}, "
                        f"cf_challenge={stats.get('isCfChallenge', False)})"
                    )

                log.info("turnstile: iframe found after %d attempts", attempts)

                clicked = await _click_widget(page, iframe_el)

                # Poll for token
                log.info("turnstile: waiting for token (timeout=%ds)", timeout)
                deadline = asyncio.get_event_loop().time() + timeout
                poll = 0
                while asyncio.get_event_loop().time() < deadline:
                    poll += 1
                    token = await _read_token(page, "cf-turnstile-response")
                    if token:
                        log.info(
                            "turnstile: token acquired len=%d (polls=%d)",
                            len(token), poll,
                        )
                        return {"token": token, "debug": {"polls": poll}}

                    if clicked and poll % 5 == 0:
                        log.info("turnstile: re-clicking (poll %d)", poll)
                        await _click_widget(page, iframe_el)

                    await asyncio.sleep(1)

                raise RuntimeError("turnstile_token_timeout")

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

                await page.goto(
                    pageurl, wait_until="domcontentloaded", timeout=30000
                )
                await asyncio.sleep(5)

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

                await page.goto(
                    url, wait_until="domcontentloaded", timeout=30000
                )
                await asyncio.sleep(3)

                deadline = asyncio.get_event_loop().time() + timeout
                while asyncio.get_event_loop().time() < deadline:
                    cookies = await context.cookies()
                    names = {c["name"] for c in cookies}
                    if "cf_clearance" in names:
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
