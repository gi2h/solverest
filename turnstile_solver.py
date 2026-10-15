"""
Turnstile + reCAPTCHA solver using Patchright + Xvfb.
v3 - Adblock shield bypass (pandafaucet & similar sites).
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
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


# =====================================================================
# STEALTH JS + ADBLOCK BYPASS
# =====================================================================
STEALTH_JS = """
// ═══════════════════════════════════════════════════════════════
// BASIC STEALTH
// ═══════════════════════════════════════════════════════════════
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

// ═══════════════════════════════════════════════════════════════
// ADBLOCK BYPASS (pandafaucet.com pf_vcheck.js)
// Fake PFVisitCheck so shield never activates
// ═══════════════════════════════════════════════════════════════
(() => {
    const fakeResult = { blocked: false, reasons: [], timing: [] };
    const fakePFVisitCheck = {
        run: async () => fakeResult,
        check: async () => fakeResult,
        isBlocked: async () => false,
    };
    try {
        Object.defineProperty(window, 'PFVisitCheck', {
            configurable: false,
            writable: false,
            value: fakePFVisitCheck,
        });
    } catch (e) {
        try { window.PFVisitCheck = fakePFVisitCheck; } catch (e2) {}
    }
})();

// Fake ad loading state (used by many adblock detectors)
try {
    Object.defineProperty(window, 'canRunAds', {
        configurable: false, get: () => true,
    });
} catch (e) {}
try {
    Object.defineProperty(window, 'canShowAds', {
        configurable: false, get: () => true,
    });
} catch (e) {}
try {
    Object.defineProperty(window, 'adsbygoogle', {
        configurable: false,
        value: Object.assign([], {
            loaded: true,
            push: function() { return 1; },
        }),
    });
} catch (e) {}

// ═══════════════════════════════════════════════════════════════
// FAKE AD ELEMENTS (common bait detection)
// ═══════════════════════════════════════════════════════════════
const _originalCreateElement = document.createElement.bind(document);
document.createElement = function(tag, options) {
    const el = _originalCreateElement(tag, options);
    // If it looks like an ad-bait element, ensure it has size
    if (typeof tag === 'string' && /ad|banner|sponsor|promo/i.test(tag)) {
        Object.defineProperty(el, 'offsetWidth', { get: () => 300 });
        Object.defineProperty(el, 'offsetHeight', { get: () => 250 });
        Object.defineProperty(el, 'offsetParent', { get: () => document.body });
    }
    return el;
};

// ═══════════════════════════════════════════════════════════════
// FAKE AD ELEMENT STYLING (any .adsbox, .ad-banner etc.)
// ═══════════════════════════════════════════════════════════════
const _addEventListener = EventTarget.prototype.addEventListener;
window.addEventListener('DOMContentLoaded', () => {
    const baits = document.querySelectorAll(
        '.adsbox, .ad-banner, .ad_banner, [class*="adsbox"], '
        + '[id*="adsbox"], [class*="banner-ad"]'
    );
    baits.forEach(el => {
        el.style.cssText += (
            ';display:block !important;'
            + 'visibility:visible !important;'
            + 'position:absolute !important;'
            + 'width:300px !important;height:250px !important;'
            + 'left:-9999px !important;top:-9999px !important;'
        );
    });
});
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


async def _hide_adblock_overlays(page):
    """Hide any adblock-detected overlay so widget can be clicked."""
    try:
        await page.evaluate("""
            () => {
                const selectors = [
                    '#adblock',
                    '[id*="adblock"]',
                    '[class*="adblock"]',
                    '[class*="overlay"]',
                ];
                selectors.forEach(sel => {
                    document.querySelectorAll(sel).forEach(el => {
                        if (el && el.style) {
                            el.style.display = 'none';
                            el.style.visibility = 'hidden';
                            el.style.pointerEvents = 'none';
                            el.style.zIndex = '-1';
                        }
                    });
                });
            }
        """)
    except Exception:
        pass


async def _page_stats(page) -> dict:
    try:
        return await page.evaluate("""
            () => {
                const iframes = Array.from(document.querySelectorAll('iframe'));
                const title = document.title;
                const url = window.location.href;
                const bodyText = document.body ? document.body.innerText : '';
                const titleLower = title.toLowerCase();
                const bodySaysJustAMoment = (
                    bodyText.toLowerCase().includes('just a moment')
                    || bodyText.toLowerCase().includes('verifying you are human')
                );
                const hasChallengeForm = !!document.querySelector(
                    '#challenge-form, .challenge-form, #challenge-stage');
                const hasCfWrapper = !!document.querySelector(
                    '#cf-challenge-running, #cf-please-wait');
                const isCfChallenge = (
                    titleLower.includes('just a moment')
                    || bodySaysJustAMoment
                    || (hasChallengeForm && hasCfWrapper)
                );
                const hasTurnstileDiv = !!document.querySelector(
                    '.cf-turnstile, [data-sitekey], [name="cf-turnstile-response"]');
                const turnstileInput = document.querySelector(
                    '[name="cf-turnstile-response"]');
                const adblockOverlay = !!document.querySelector(
                    '#adblock:not(.hide), [class*="adblock"]:not(.hide)');
                return {
                    title: title,
                    url: url,
                    iframeCount: iframes.length,
                    bodyLen: document.body ? document.body.innerHTML.length : 0,
                    isCfChallenge: isCfChallenge,
                    hasTurnstileDiv: hasTurnstileDiv,
                    hasTurnstileInput: !!turnstileInput,
                    inputValue: turnstileInput ? (turnstileInput.value || '').length : 0,
                    adblockOverlayVisible: adblockOverlay,
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
            try:
                context = await _new_context(browser)
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
                    raise RuntimeError(f"navigation_failed: {e}")

                log.info("turnstile: page loaded url=%s", page.url)

                # Give JS time to boot
                await asyncio.sleep(3)

                # Hide adblock overlay if it appeared
                await _hide_adblock_overlays(page)

                stats = await _page_stats(page)
                log.info(
                    "turnstile: stats title=%r iframes=%d cf=%s "
                    "turnstileDiv=%s adblockOverlay=%s",
                    stats.get("title", "")[:60],
                    stats.get("iframeCount", 0),
                    stats.get("isCfChallenge", False),
                    stats.get("hasTurnstileDiv", False),
                    stats.get("adblockOverlayVisible", False),
                )

                # Early token?
                token = await _read_token(page, "cf-turnstile-response")
                if token:
                    log.info("turnstile: token present early len=%d", len(token))
                    return {"token": token, "debug": {"early": True}}

                # Real CF interstitial?
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
                            return {"token": token, "debug": {"cf_pass": True}}

                # Main polling loop
                log.info("turnstile: waiting for token (timeout=%ds)", timeout)
                deadline = asyncio.get_event_loop().time() + timeout
                iframe_el = None
                clicked = False
                attempts = 0
                last_log = 0

                while asyncio.get_event_loop().time() < deadline:
                    attempts += 1

                    # Re-hide adblock overlay periodically
                    if attempts % 15 == 0:
                        await _hide_adblock_overlays(page)

                    # Priority 1: token check
                    token = await _read_token(page, "cf-turnstile-response")
                    if token:
                        log.info(
                            "turnstile: token acquired len=%d at attempt %d",
                            len(token), attempts,
                        )
                        return {"token": token, "debug": {"attempts": attempts}}

                    # Priority 2: find iframe + click
                    if not iframe_el:
                        iframe_el = await _find_turnstile_iframe(page)
                    if iframe_el and not clicked:
                        clicked = await _click_widget(page, iframe_el)

                    # Periodic logging
                    if attempts - last_log >= 10:
                        last_log = attempts
                        s = await _page_stats(page)
                        log.info(
                            "turnstile attempt %d: iframes=%d cf=%s "
                            "input_len=%d adblockOverlay=%s",
                            attempts,
                            s.get("iframeCount", 0),
                            s.get("isCfChallenge", False),
                            s.get("inputValue", 0),
                            s.get("adblockOverlayVisible", False),
                        )

                    await asyncio.sleep(1)

                stats = await _page_stats(page)
                raise RuntimeError(
                    f"turnstile_token_timeout "
                    f"(url={page.url}, iframes={stats.get('iframeCount', 0)}, "
                    f"cf={stats.get('isCfChallenge', False)}, "
                    f"adblockOverlay={stats.get('adblockOverlayVisible', False)})"
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
                await asyncio.sleep(3)
                await _hide_adblock_overlays(page)

                iframe_sel = (
                    'iframe[src*="google.com/recaptcha"], '
                    'iframe[title*="recaptcha"]'
                )
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
