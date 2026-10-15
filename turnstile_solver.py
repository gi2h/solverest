"""
Turnstile + reCAPTCHA + Cloudflare cookie solver using Patchright.

Patchright = patched Playwright that avoids CDP detection.
It launches a real Chromium, navigates, waits for widget, clicks it,
and extracts the token from the hidden input field.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

log = logging.getLogger("solver")
logging.basicConfig(
    level=logging.INFO,
    format="[solver] %(message)s",
)


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def _launch_args() -> list:
    return [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-features=IsolateOrigins,site-per-process",
    ]


async def _new_context(browser, proxy: Optional[str] = None):
    ctx_kwargs = {
        "viewport": {"width": 1280, "height": 800},
        "user_agent": USER_AGENT,
        "locale": "en-US",
        "timezone_id": "America/New_York",
        "extra_http_headers": {
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    return await browser.new_context(**ctx_kwargs)


async def _read_token(page, input_name: str) -> Optional[str]:
    """Read the current value of a hidden input (token)."""
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


async def _click_widget(page, iframe_selector: str) -> bool:
    """Try to click the widget checkbox via mouse coordinates."""
    iframe_el = await page.query_selector(iframe_selector)
    if not iframe_el:
        log.info("click: iframe not found")
        return False

    box = await iframe_el.bounding_box()
    if not box:
        log.info("click: no bounding box")
        return False

    log.info(
        "click: iframe at x=%.0f y=%.0f w=%.0f h=%.0f",
        box["x"], box["y"], box["width"], box["height"],
    )

    # Widget layout: checkbox at ~30px left, center vertical
    cx = box["x"] + 30
    cy = box["y"] + (box["height"] / 2)

    # Human-like mouse approach
    await page.mouse.move(cx - 40, cy - 30, steps=8)
    await asyncio.sleep(0.25)
    await page.mouse.move(cx, cy, steps=12)
    await asyncio.sleep(0.2)
    await page.mouse.click(cx, cy)
    await asyncio.sleep(0.6)

    log.info("click: done at (%.0f, %.0f)", cx, cy)
    return True


async def solve_turnstile(
    sitekey: str,
    pageurl: str,
    timeout: int = 90,
    proxy: Optional[str] = None,
    headless: bool = True,
) -> str:
    """
    Solve Cloudflare Turnstile widget.

    Returns token string. Raises RuntimeError on failure.
    """
    try:
        from patchright.async_api import async_playwright
    except ImportError as e:
        raise RuntimeError(f"patchright_not_installed: {e}")

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
            context = await _new_context(browser, proxy)
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

            # Check if token already present (auto-solve in <1s)
            token = await _read_token(page, "cf-turnstile-response")
            if token:
                log.info("turnstile: token present immediately len=%d", len(token))
                return token

            # Wait for iframe to load
            iframe_sel = (
                'iframe[src*="challenges.cloudflare.com"], '
                'iframe[src*="turnstile"]'
            )
            try:
                await page.wait_for_selector(iframe_sel, timeout=15000)
                log.info("turnstile: iframe detected")
            except Exception:
                log.info("turnstile: no iframe after 15s, re-checking token")
                token = await _read_token(page, "cf-turnstile-response")
                if token:
                    return token
                raise RuntimeError("turnstile_iframe_not_found")

            # Try clicking checkbox
            clicked = await _click_widget(page, iframe_sel)
            log.info("turnstile: click result=%s", clicked)

            # Poll for token
            deadline = asyncio.get_event_loop().time() + timeout
            attempt = 0
            while asyncio.get_event_loop().time() < deadline:
                attempt += 1
                token = await _read_token(page, "cf-turnstile-response")
                if token:
                    log.info(
                        "turnstile: token acquired len=%d (attempts=%d)",
                        len(token), attempt,
                    )
                    return token
                # Re-click every 3 attempts
                if clicked and attempt % 3 == 0:
                    log.info("turnstile: re-click attempt %d", attempt)
                    await _click_widget(page, iframe_sel)
                await asyncio.sleep(1.0)

            raise RuntimeError("turnstile_token_timeout")

        finally:
            try:
                await browser.close()
            except Exception:
                pass


async def solve_recaptcha(
    sitekey: str,
    pageurl: str,
    timeout: int = 90,
    proxy: Optional[str] = None,
    headless: bool = True,
) -> str:
    """Solve reCAPTCHA v2 widget."""
    try:
        from patchright.async_api import async_playwright
    except ImportError as e:
        raise RuntimeError(f"patchright_not_installed: {e}")

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
            context = await _new_context(browser, proxy)
            page = await context.new_page()

            log.info("recaptcha: navigating to %s", pageurl)
            try:
                await page.goto(
                    pageurl, wait_until="domcontentloaded", timeout=30000
                )
            except Exception as e:
                raise RuntimeError(f"navigation_failed: {e}")

            # Wait for recaptcha anchor iframe
            try:
                await page.wait_for_selector(
                    'iframe[src*="google.com/recaptcha"], '
                    'iframe[title*="recaptcha"]',
                    timeout=15000,
                )
                log.info("recaptcha: iframe detected")
            except Exception:
                raise RuntimeError("recaptcha_iframe_not_found")

            # Try clicking
            iframe_sel = (
                'iframe[src*="google.com/recaptcha"], '
                'iframe[title*="recaptcha"]'
            )
            await _click_widget(page, iframe_sel)

            deadline = asyncio.get_event_loop().time() + timeout
            attempt = 0
            while asyncio.get_event_loop().time() < deadline:
                attempt += 1
                token = await _read_token(page, "g-recaptcha-response")
                if token:
                    log.info("recaptcha: token len=%d", len(token))
                    return token
                if attempt % 3 == 0:
                    await _click_widget(page, iframe_sel)
                await asyncio.sleep(1.0)

            raise RuntimeError("recaptcha_token_timeout")

        finally:
            try:
                await browser.close()
            except Exception:
                pass


async def get_cloudflare_cookie(
    url: str,
    timeout: int = 60,
    proxy: Optional[str] = None,
    headless: bool = True,
) -> dict:
    """
    Fetch URL and return cookies + user-agent.
    Useful for Cloudflare bot management sites (not Turnstile widget).
    """
    try:
        from patchright.async_api import async_playwright
    except ImportError as e:
        raise RuntimeError(f"patchright_not_installed: {e}")

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
            context = await _new_context(browser, proxy)
            page = await context.new_page()

            log.info("cf-cookie: navigating to %s", url)
            try:
                await page.goto(
                    url, wait_until="domcontentloaded", timeout=30000
                )
            except Exception as e:
                raise RuntimeError(f"navigation_failed: {e}")

            # Wait a bit for Cloudflare challenge to resolve
            await asyncio.sleep(3)

            # Wait for potential JS challenge
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
