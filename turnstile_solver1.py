"""
Turnstile solver — v7 (Final)
- Shadow DOM aware (via page.frames)
- Single click logic (waits for token, prevents click-spamming)
- headless=False (xvfb) by default
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
"""

def _launch_args() -> list:
    return [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
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
            log.info("turnstile: added %d cookies to context", len(cookies_list))
        except Exception as e:
            log.warning("add_cookies failed: %s", e)
    return ctx

async def _read_token(page, input_name: str = "cf-turnstile-response") -> Optional[str]:
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

async def _has_cf_widget(page) -> bool:
    try:
        return await page.evaluate("""() => {
            if (document.querySelector('iframe[src*="challenges.cloudflare.com"]')) return true;
            if (document.querySelector('.cf-turnstile')) return true;
            if (document.querySelector('[data-sitekey]')) return true;
            if (document.querySelector('[name="cf-turnstile-response"]')) return true;
            if (window.turnstile) return true;
            return false;
        }""")
    except Exception:
        return False

async def _page_stats(page) -> dict:
    try:
        return await page.evaluate("""
            () => {
                const iframes = Array.from(document.querySelectorAll('iframe'));
                return {
                    title: document.title,
                    url: window.location.href,
                    iframeCount: iframes.length,
                    hasCfScript: !!document.querySelector('script[src*="challenges.cloudflare.com"]'),
                    hasTurnstileGlobal: typeof window.turnstile !== 'undefined',
                    inputLen: (() => {
                        const el = document.querySelector('[name="cf-turnstile-response"]');
                        return el ? (el.value || '').length : 0;
                    })(),
                };
            }
        """)
    except Exception as e:
        return {"error": str(e)[:200]}

async def _find_turnstile_iframe(page):
    """
    Cari iframe dengan menembus Shadow DOM via page.frames.
    """
    for frame in page.frames:
        if "challenges.cloudflare.com" in (frame.url or ""):
            try:
                el = await frame.frame_element()
                if el:
                    await el.scroll_into_view_if_needed()
                    await asyncio.sleep(0.3)
                    box = await el.bounding_box()
                    if box and box["width"] > 10 and box["height"] > 10:
                        log.info("found iframe via frame_element")
                        return el
            except Exception:
                pass
    return None

async def _click_widget(page, iframe_el) -> bool:
    try:
        await iframe_el.scroll_into_view_if_needed()
        await asyncio.sleep(0.4)
        
        box = await iframe_el.bounding_box()
        if not box:
            return False
            
        # Jika iframe terlalu besar (misal fullscreen challenge), jangan klik asal
        if box["width"] > 400 or box["height"] > 200:
            log.warning("click: iframe terlalu besar (challenge mode?), skip klik.")
            return False

        # Klik di area checkbox (kiri tengah)
        cx = box["x"] + random.uniform(25, 35)
        cy = box["y"] + (box["height"] / 2) + random.uniform(-3, 3)

        # Gerakan mouse natural
        await page.mouse.move(cx - 80, cy - 60, steps=random.randint(10, 20))
        await asyncio.sleep(random.uniform(0.15, 0.3))
        await page.mouse.move(cx, cy, steps=random.randint(12, 25))
        await asyncio.sleep(random.uniform(0.1, 0.2))
        
        await page.mouse.click(cx, cy)
        log.info("click: success at (%.0f, %.0f)", cx, cy)
        return True
    except Exception as e:
        log.warning("click failed: %s", e)
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
        log.info("turnstile: launching (headless=%s, cookies=%d)", headless, len(parsed_cookies))
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
                context = await _new_context(browser, cookies_list=parsed_cookies)
                await context.add_init_script(STEALTH_JS)
                page = await context.new_page()

                log.info("turnstile: navigating to %s", pageurl)
                try:
                    await page.goto(pageurl, wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    return _fail(f"navigation_failed: {e}", started)

                log.info("turnstile: page loaded url=%s", page.url)
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                await asyncio.sleep(2)

                # FASE 1: Tunggu widget muncul
                widget_appeared = False
                for i in range(20):
                    await asyncio.sleep(1)
                    if await _has_cf_widget(page):
                        widget_appeared = True
                        log.info("turnstile: widget detected after %ds", i + 1)
                        break

                if not widget_appeared:
                    return _fail("turnstile_widget_not_found", started)

                # FASE 2: Cek token
                token = await _read_token(page)
                if token:
                    return _ok(token, 0, started)

                # FASE 3: Klik cerdas & Tunggu token
                deadline = asyncio.get_event_loop().time() + timeout
                iframe_el = None
                clicks_done = 0
                attempts = 0
                last_log = 0
                last_click_time = 0.0

                while asyncio.get_event_loop().time() < deadline:
                    attempts += 1

                    token = await _read_token(page)
                    if token:
                        log.info("turnstile: token acquired len=%d at attempt %d", len(token), attempts)
                        return _ok(token, attempts, started)

                    now = asyncio.get_event_loop().time()
                    
                    # Logika Klik: Maksimal 1x klik, tunggu minimal 10 detik sebelum klik lagi jika gagal
                    if clicks_done < 2 and (now - last_click_time) > 10.0:
                        if iframe_el is None:
                            iframe_el = await _find_turnstile_iframe(page)
                            
                        if iframe_el is not None:
                            if await _click_widget(page, iframe_el):
                                clicks_done += 1
                                last_click_time = now
                                iframe_el = None # Reset agar fresh
                    
                    if attempts - last_log >= 10:
                        last_log = attempts
                        s = await _page_stats(page)
                        log.info(
                            "turnstile attempt %d: input_len=%d clicks=%d",
                            attempts, s.get("inputLen", 0), clicks_done
                        )

                    await asyncio.sleep(1)

                return _fail("turnstile_token_timeout", started, attempts)

            finally:
                try:
                    await page.close()
                except Exception:
                    pass
                try:
                    await context.close()
                except Exception:
                    pass
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
