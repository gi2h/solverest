"""
Turnstile + reCAPTCHA solver using Patchright + Xvfb.
v2.2 - CSP bypass, network logging, manual render fallback.
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

# === CLEANUP CONFIG ===
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


def _setup_page_listeners(page):
    """Log error browser, CSP, dan request yang gagal."""
    page.on("console", lambda msg: log.info(f"[console] {msg.type}: {msg.text}"))
    page.on("pageerror", lambda err: log.info(f"[pageerror] {err}"))
    page.on("requestfailed", lambda req: log.info(f"[reqfail] {req.url[:80]} - {req.failure}"))


async def _bypass_csp(route):
    """Intersepsi dan hapus CSP agar iframe Cloudflare bisa dimuat."""
    try:
        response = await route.fetch()
        headers = response.headers
        headers.pop("content-security-policy", None)
        headers.pop("content-security-policy-report-only", None)
        await route.fulfill(response=response, headers=headers)
    except Exception:
        await route.continue_()


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


async def _safe_cleanup(page, context, browser):
    """Tutup page -> context -> browser berurutan dengan timeout."""
    if page is not None:
        try:
            await asyncio.wait_for(page.close(run_before_unload=False), timeout=PAGE_CLOSE_TIMEOUT)
        except Exception as e:
            log.debug("page.close skipped: %s", e)

    if context is not None:
        try:
            await asyncio.wait_for(context.close(), timeout=CONTEXT_CLOSE_TIMEOUT)
        except Exception as e:
            log.debug("context.close skipped: %s", e)

    if browser is not None:
        try:
            await asyncio.wait_for(browser.close(), timeout=BROWSER_CLOSE_TIMEOUT)
        except Exception as e:
            log.debug("browser.close skipped: %s", e)


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
                const hasChallengeForm = !!document.querySelector('#challenge-form, .challenge-form, #challenge-stage');
                const hasCfWrapper = !!document.querySelector('#cf-challenge-running, #cf-please-wait');
                const bodySaysJustAMoment = bodyText.toLowerCase().includes('just a moment') || bodyText.toLowerCase().includes('verifying you are human');

                const isCfChallenge = (
                    titleLower.includes('just a moment') ||
                    bodySaysJustAMoment ||
                    (hasChallengeForm && hasCfWrapper)
                );

                const hasTurnstileDiv = !!document.querySelector('.cf-turnstile, [data-sitekey], [name="cf-turnstile-response"]');
                const turnstileInput = document.querySelector('[name="cf-turnstile-response"]');

                return {
                    title: title,
                    url: url,
                    iframeCount: iframes.length,
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
    """Cari iframe via selector, termasuk yang ada di dalam Shadow DOM."""
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

    # Fallback: cek di dalam Shadow DOM (jika ada)
    try:
        el = await page.evaluate_handle("""
            () => {
                const findInShadow = (root) => {
                    const iframe = root.querySelector('iframe[src*="challenges.cloudflare.com"]');
                    if (iframe) return iframe;
                    const children = root.querySelectorAll('*');
                    for (let child of children) {
                        if (child.shadowRoot) {
                            const found = findInShadow(child.shadowRoot);
                            if (found) return found;
                        }
                    }
                    return null;
                };
                return findInShadow(document);
            }
        """)
        if el:
            return await el.as_element()
    except Exception:
        pass

    return None


async def _click_widget(page, iframe_el) -> bool:
    try:
        box = await iframe_el.bounding_box()
        if not box:
            # Fallback: Klik area tengah div .cf-turnstile
            div = await page.query_selector('.cf-turnstile, [data-sitekey]')
            if div:
                box = await div.bounding_box()
        
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
            context = None
            page = None
            try:
                context = await _new_context(browser)
                # Daftarkan CSP bypass & logger
                await context.route("**/*", _bypass_csp)
                await context.add_init_script(STEALTH_JS)
                page = await context.new_page()
                _setup_page_listeners(page)

                log.info("turnstile: navigating to %s", pageurl)
                try:
                    await page.goto(pageurl, wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    raise RuntimeError(f"navigation_failed: {e}")

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

                token = await _read_token(page, "cf-turnstile-response")
                if token:
                    log.info("turnstile: token present early len=%d", len(token))
                    return {"token": token, "debug": {"early": True}}

                if stats.get("isCfChallenge"):
                    log.info("turnstile: real CF challenge detected, waiting up to 20s")
                    cf_deadline = asyncio.get_event_loop().time() + 20
                    while asyncio.get_event_loop().time() < cf_deadline:
                        await asyncio.sleep(2)
                        new_stats = await _page_stats(page)
                        if not new_stats.get("isCfChallenge"):
                            break
                        token = await _read_token(page, "cf-turnstile-response")
                        if token:
                            return {"token": token, "debug": {"cf_pass": True}}

                log.info("turnstile: waiting for token (timeout=%ds)", timeout)
                deadline = asyncio.get_event_loop().time() + timeout
                iframe_el = None
                clicked = False
                attempts = 0
                last_log = 0
                render_attempted = False

                while asyncio.get_event_loop().time() < deadline:
                    attempts += 1

                    token = await _read_token(page, "cf-turnstile-response")
                    if token:
                        log.info("turnstile: token acquired len=%d at attempt %d", len(token), attempts)
                        return {"token": token, "debug": {"attempts": attempts}}

                    if not iframe_el:
                        iframe_el = await _find_turnstile_iframe(page)
                        if iframe_el and not clicked:
                            clicked = await _click_widget(page, iframe_el)

                    # Fallback: Paksa render script Turnstile jika iframe=0 setelah 10 detik
                    if not iframe_el and not render_attempted and attempts >= 10:
                        render_attempted = True
                        log.info("turnstile: no iframe detected, forcing manual render...")
                        await page.evaluate("""
                            () => {
                                if (window.turnstile && window.turnstile.render) {
                                    const div = document.querySelector('.cf-turnstile');
                                    if (div && div.innerHTML.trim() === '') {
                                        try {
                                            const key = div.getAttribute('data-sitekey');
                                            if (key) window.turnstile.render(div, { sitekey: key });
                                        } catch(e) {}
                                    }
                                }
                            }
                        """)

                    if attempts - last_log >= 10:
                        last_log = attempts
                        s = await _page_stats(page)
                        log.info(
                            "turnstile attempt %d: iframes=%d cf=%s input_len=%d",
                            attempts, s.get("iframeCount", 0), s.get("isCfChallenge", False), s.get("inputValue", 0),
                        )

                    await asyncio.sleep(1)

                stats = await _page_stats(page)
                raise RuntimeError(
                    f"turnstile_token_timeout (url={page.url}, iframes={stats.get('iframeCount', 0)}, cf={stats.get('isCfChallenge', False)})"
                )

            finally:
                await _safe_cleanup(page, context, browser)

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
            context = None
            page = None
            try:
                context = await _new_context(browser)
                await context.route("**/*", _bypass_csp)
                await context.add_init_script(STEALTH_JS)
                page = await context.new_page()
                _setup_page_listeners(page)

                await page.goto(pageurl, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(3)

                iframe_sel = 'iframe[src*="google.com/recaptcha"], iframe[title*="recaptcha"]'
                deadline = asyncio.get_event_loop().time() + timeout
                clicked = False
                iframe_el = None

                while asyncio.get_event_loop().time() < deadline:
                    token = await _read_token(page, "g-recaptcha-response")
                    if token:
                        return {"token": token, "debug": {}}

                    if not iframe_el:
                        iframe_el = await page.query_selector(iframe_sel)
                        if iframe_el and not clicked:
                            clicked = await _click_widget(page, iframe_el)

                    await asyncio.sleep(1)

                raise RuntimeError("recaptcha_token_timeout")

            finally:
                await _safe_cleanup(page, context, browser)
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
            context = None
            page = None
            try:
                context = await _new_context(browser)
                await context.route("**/*", _bypass_csp)
                await context.add_init_script(STEALTH_JS)
                page = await context.new_page()
                _setup_page_listeners(page)

                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
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
                await _safe_cleanup(page, context, browser)
    finally:
        if display:
            try:
                display.stop()
            except Exception:
                pass
