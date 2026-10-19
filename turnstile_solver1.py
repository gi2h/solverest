# turnstile_solver1.py
"""
Turnstile solver berbasis patchright (Chromium stealth).
Signature lengkap, toleran terhadap variasi widget Turnstile.
"""

import asyncio
import os
import random
import time
from typing import List, Optional, Union
from urllib.parse import urlparse

from patchright.async_api import async_playwright, Frame, Page

# ---------------------------------------------------------------
# Konstanta
# ---------------------------------------------------------------

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
]

TURNSTILE_HINTS = (
    "challenges.cloudflare.com",
    "/cdn-cgi/challenge-platform/",
)

TOKEN_SELECTOR = (
    'input[name="cf-turnstile-response"], '
    'textarea[name="cf-turnstile-response"]'
)

WIDGET_SELECTOR = (
    'iframe[src*="challenges.cloudflare.com"], '
    '.cf-turnstile, '
    '[data-sitekey], '
    f'{TOKEN_SELECTOR}'
)

CHECKBOX_SELECTORS = [
    'input[type="checkbox"]',
    'label.cb-lb',
    '.cb-c input',
    '#challenge-stage input',
    '#challenge-stage label',
    'body > div > div > label',
    'label[for]',
]


# ---------------------------------------------------------------
# Helper
# ---------------------------------------------------------------

def _parse_cookies(cookies, url: str) -> List[dict]:
    """Terima cookie string 'a=1; b=2' atau list dict Playwright."""
    if not cookies:
        return []

    host = urlparse(url).hostname or ""
    result: List[dict] = []

    if isinstance(cookies, list):
        for c in cookies:
            if not isinstance(c, dict) or "name" not in c:
                continue
            c = dict(c)
            c.setdefault("value", "")
            c.setdefault("path", "/")
            if "domain" not in c and "url" not in c:
                c["domain"] = host
            result.append(c)
        return result

    for part in str(cookies).split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        result.append(
            {
                "name": name.strip(),
                "value": value.strip(),
                "domain": host,
                "path": "/",
            }
        )
    return result


async def _find_turnstile_frame(page: Page) -> Optional[Frame]:
    for frame in page.frames:
        url = frame.url or ""
        if any(h in url for h in TURNSTILE_HINTS):
            return frame
    return None


async def _read_token(page: Page) -> Optional[str]:
    try:
        return await page.evaluate(
            """() => {
                const el = document.querySelector(
                    'input[name="cf-turnstile-response"], ' +
                    'textarea[name="cf-turnstile-response"]'
                );
                if (!el) return null;
                const v = (el.value || '').trim();
                return v.length >= 20 ? v : null;
            }"""
        )
    except Exception:
        return None


async def _click_checkbox(frame: Frame) -> bool:
    for sel in CHECKBOX_SELECTORS:
        try:
            el = await frame.query_selector(sel)
            if el is None:
                continue
            try:
                await el.scroll_into_view_if_needed(timeout=1000)
            except Exception:
                pass
            await el.click(timeout=2000, force=True)
            return True
        except Exception:
            continue
    return False


async def _click_checkbox_by_coords(page: Page) -> bool:
    try:
        iframe = await page.query_selector(
            'iframe[src*="challenges.cloudflare.com"]'
        )
        if iframe is None:
            return False
        box = await iframe.bounding_box()
        if not box:
            return False
        await page.mouse.click(
            box["x"] + 30, box["y"] + box["height"] / 2
        )
        return True
    except Exception:
        return False


# ---------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------

async def solve_turnstile(
    sitekey: str = "",
    pageurl: str = "",
    *,
    proxy: Optional[str] = None,
    cookies: Optional[Union[str, List[dict]]] = None,
    user_agent: Optional[str] = None,
    timeout: int = 90,
    headless: Optional[bool] = None,
    max_attempts: int = 45,
) -> dict:
    """
    Buka `pageurl`, tunggu widget Turnstile, klik checkbox, ambil token.

    Return:
        {"success": True, "token": "...", "attempts": N, "elapsed_ms": M}
        {"success": False, "error": "...", "attempts": N, "elapsed_ms": M}
    """
    if not pageurl:
        return {"success": False, "error": "pageurl_required"}

    if headless is None:
        headless = os.environ.get("HEADLESS", "true").lower() != "false"

    started = time.time()
    timeout_ms = timeout * 1000

    async with async_playwright() as p:
        launch_kwargs = {
            "headless": headless,
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--disable-gpu",
            ],
        }
        if proxy:
            # format: http://user:pass@host:port atau host:port
            launch_kwargs["proxy"] = {"server": proxy}

        browser = await p.chromium.launch(**launch_kwargs)

        context = await browser.new_context(
            user_agent=user_agent or random.choice(USER_AGENTS),
            viewport={"width": 1366, "height": 820},
            locale="en-US",
            timezone_id="America/New_York",
            ignore_https_errors=True,
        )

        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

        parsed_cookies = _parse_cookies(cookies, pageurl)
        if parsed_cookies:
            try:
                await context.add_cookies(parsed_cookies)
            except Exception as e:
                print(f"[solver] add_cookies failed: {e}")

        page = await context.new_page()

        try:
            try:
                await page.goto(
                    pageurl, wait_until="domcontentloaded", timeout=timeout_ms
                )
            except Exception as e:
                return {
                    "success": False,
                    "error": f"navigation_failed: {type(e).__name__}: {e}",
                    "elapsed_ms": int((time.time() - started) * 1000),
                }

            # Tunggu widget muncul
            try:
                await page.wait_for_selector(
                    WIDGET_SELECTOR, timeout=timeout_ms
                )
            except Exception:
                token = await _read_token(page)
                if token:
                    return {
                        "success": True,
                        "token": token,
                        "attempts": 0,
                        "elapsed_ms": int((time.time() - started) * 1000),
                    }
                return {
                    "success": False,
                    "error": "turnstile_widget_not_found",
                    "elapsed_ms": int((time.time() - started) * 1000),
                }

            last_iframe_count = -1

            for attempt in range(1, max_attempts + 1):
                token = await _read_token(page)
                if token:
                    return {
                        "success": True,
                        "token": token,
                        "attempts": attempt,
                        "elapsed_ms": int((time.time() - started) * 1000),
                    }

                frame = await _find_turnstile_frame(page)
                iframe_count = sum(
                    1
                    for f in page.frames
                    if any(h in (f.url or "") for h in TURNSTILE_HINTS)
                )

                if attempt == 1 or iframe_count != last_iframe_count:
                    print(
                        f"[solver] turnstile attempt {attempt}: "
                        f"iframes={iframe_count} input_len=0"
                    )
                    last_iframe_count = iframe_count

                clicked = False
                if frame is not None:
                    clicked = await _click_checkbox(frame)
                if not clicked:
                    await _click_checkbox_by_coords(page)

                # kadang token muncul tanpa klik (managed challenge)
                await asyncio.sleep(0.8 + random.random() * 0.7)

            token = await _read_token(page)
            if token:
                return {
                    "success": True,
                    "token": token,
                    "attempts": max_attempts,
                    "elapsed_ms": int((time.time() - started) * 1000),
                }
            return {
                "success": False,
                "error": "timeout_waiting_token",
                "attempts": max_attempts,
                "elapsed_ms": int((time.time() - started) * 1000),
            }

        finally:
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass
