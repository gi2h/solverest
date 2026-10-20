"""
Turnstile solver — v5 (clean & realistic)
────────────────────────────────────────────────────────────────────────
Prinsip:
  - Satu UA Chrome konsisten (context == headers).
  - Non-headless default (butuh xvfb) supaya Turnstile render.
  - Argumen minimal — tidak ada flag yang bikin browser "aneh".
  - Toleran: tunggu widget hingga 20 detik, retry klik beberapa kali.
  - Wait `networkidle` + delay agar JS selesai eksekusi (PlayNXC butuh).
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


# ── Satu UA konsisten, Chrome 131 di Windows 10 ─────────────────────
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# ── Stealth minimal. Jangan berlebihan — CF mendeteksi patch aneh. ─
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
"""


# ── Args minimal. Flag CF-friendly, tidak terlalu agresif. ─────────
def _launch_args() -> list:
    return [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
    ]


def _start_xvfb():
    from pyvirtualdisplay import Display
    display = Display(visible=0, size=(1280, 800))
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
    """
    Context realistis:
      - UA konsisten (Windows Chrome 131)
      - platform header sesuai UA (Windows)
      - locale & timezone Amerika
      - viewport realistis
    """
    ctx = await browser.new_context(
        viewport={"width": 1366, "height": 800},
        user_agent=USER_AGENT,
        locale="en-US",
        timezone_id="America/New_York",
        device_scale_factor=1,
        is_mobile=False,
        has_touch=False,
        java_script_enabled=True,
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", '
                         '"Not_A Brand";v="24"',
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
            if (document.querySelector('iframe[src*="turnstile"]')) return true;
            if (document.querySelector('iframe[id*="cf-chl-widget"]')) return true;
            if (document.querySelector('.cf-turnstile')) return true;
            if (document.querySelector('[data-sitekey]')) return true;
            if (document.querySelector('[name="cf-turnstile-response"]')) return true;
            if (document.querySelector('script[src*="challenges.cloudflare.com/turnstile"]')) return true;
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
                    iframeSrcs: iframes.map(f => (f.src || '').slice(0, 120)).slice(0, 6),
                    hasCfScript: !!document.querySelector('script[src*="challenges.cloudflare.com"]'),
                    hasTurnstileGlobal: typeof window.turnstile !== 'undefined',
                    hasCfInput: !!document.querySelector('[name="cf-turnstile-response"]'),
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
    """Klik dengan gerakan mouse manusiawi (jitter + steps)."""
    try:
        box = await iframe_el.bounding_box()
        if not box:
            return False
        # Titik di dalam checkbox, sedikit offset acak
        cx = box["x"] + random.uniform(25, 40)
        cy = box["y"] + (box["height"] / 2) + random.uniform(-3, 3)

        # Gerak cursor masuk dari luar widget
        await page.mouse.move(cx - 80, cy - 60, steps=random.randint(10, 20))
        await asyncio.sleep(random.uniform(0.15, 0.35))
        await page.mouse.move(cx, cy, steps=random.randint(12, 25))
        await asyncio.sleep(random.uniform(0.1, 0.25))
        await page.mouse.click(cx, cy)
        await asyncio.sleep(random.uniform(0.4, 0.7))
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


# ════════════════════════════════════════════════════════════════════
# MAIN ENTRY
# ════════════════════════════════════════════════════════════════════
async def solve_turnstile(
    sitekey: str = "",
    pageurl: str = "",
    timeout: int = 90,
    proxy: Optional[str] = None,
    headless: Optional[bool] = None,
    cookies: Optional[Union[str, List[dict]]] = None,
    user_agent: Optional[str] = None,   # diterima tapi diabaikan (UA konsisten)
) -> dict:
    started = time.time()
    if not pageurl:
        return _fail("pageurl_required", started)

    # Default: non-headless. CF Turnstile lebih sering render di non-headless.
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
        log.info(
            "turnstile: launching (headless=%s, cookies=%d, ua=%s)",
            headless, len(parsed_cookies), USER_AGENT[:60],
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
                context = await _new_context(browser, cookies_list=parsed_cookies)
                await context.add_init_script(STEALTH_JS)
                page = await context.new_page()

                log.info("turnstile: navigating to %s", pageurl)
                try:
                    # domcontentloaded dulu, lalu beri waktu untuk network
                    await page.goto(pageurl, wait_until="domcontentloaded", timeout=30000)
                except Exception as e:
                    return _fail(f"navigation_failed: {e}", started)

                log.info("turnstile: page loaded url=%s", page.url)

                # Beri JS + ads + delay 3 detik PlayNXC waktu render
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                await asyncio.sleep(2)

                # ── FASE 1: tunggu widget muncul (maks 20 detik) ──
                widget_appeared = False
                for i in range(20):
                    await asyncio.sleep(1)
                    if await _has_cf_widget(page):
                        widget_appeared = True
                        log.info("turnstile: widget detected after %ds", i + 1)
                        break

                if not widget_appeared:
                    stats = await _page_stats(page)
                    log.warning(
                        "turnstile: widget never appeared in 20s "
                        "(url=%s, iframes=%d, cfScript=%s, tsGlobal=%s, title=%r)",
                        page.url,
                        stats.get("iframeCount", 0),
                        stats.get("hasCfScript", False),
                        stats.get("hasTurnstileGlobal", False),
                        stats.get("title", "")[:60],
                    )
                    return _fail(
                        "turnstile_widget_not_found "
                        f"(url={page.url}, iframes={stats.get('iframeCount', 0)}, "
                        f"cfScript={stats.get('hasCfScript', False)}, "
                        f"tsGlobal={stats.get('hasTurnstileGlobal', False)}, "
                        f"title={stats.get('title', '')[:60]!r})",
                        started,
                    )

                # ── FASE 2: token mungkin sudah ada (managed auto-pass) ──
                token = await _read_token(page)
                if token:
                    log.info("turnstile: token present early len=%d", len(token))
                    return _ok(token, 0, started)

                # ── FASE 3: loop tunggu token; klik sampai 3x ──
                deadline = asyncio.get_event_loop().time() + timeout
                iframe_el = None
                clicks_done = 0
                max_clicks = 3
                attempts = 0
                last_log = 0
                next_click_at = 0.0

                while asyncio.get_event_loop().time() < deadline:
                    attempts += 1

                    token = await _read_token(page)
                    if token:
                        log.info("turnstile: token acquired len=%d at attempt %d",
                                 len(token), attempts)
                        return _ok(token, attempts, started)

                    # Coba klik maksimal 3x, dengan jeda
                    now = asyncio.get_event_loop().time()
                    if clicks_done < max_clicks and now >= next_click_at:
                        if iframe_el is None:
                            iframe_el = await _find_turnstile_iframe(page)
                        if iframe_el is not None:
                            if await _click_widget(page, iframe_el):
                                clicks_done += 1
                                next_click_at = now + random.uniform(3.0, 5.0)
                                # Re-fetch iframe tiap klik (bisa re-mount)
                                iframe_el = None
                        else:
                            # iframe belum ada — coba lagi nanti
                            next_click_at = now + 2.0

                    if attempts - last_log >= 10:
                        last_log = attempts
                        s = await _page_stats(page)
                        log.info(
                            "turnstile attempt %d: iframes=%d cfScript=%s tsGlobal=%s "
                            "input_len=%d clicks=%d",
                            attempts,
                            s.get("iframeCount", 0),
                            s.get("hasCfScript", False),
                            s.get("hasTurnstileGlobal", False),
                            s.get("inputLen", 0),
                            clicks_done,
                        )

                    await asyncio.sleep(1)

                stats = await _page_stats(page)
                return _fail(
                    "turnstile_token_timeout "
                    f"(url={page.url}, iframes={stats.get('iframeCount', 0)}, "
                    f"cfScript={stats.get('hasCfScript', False)}, "
                    f"tsGlobal={stats.get('hasTurnstileGlobal', False)}, "
                    f"input_len={stats.get('inputLen', 0)}, clicks={clicks_done})",
                    started, attempts,
                )

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


# ════════════════════════════════════════════════════════════════════
# reCAPTCHA (dibiarkan, konsisten dengan solve_turnstile)
# ════════════════════════════════════════════════════════════════════
async def solve_recaptcha(sitekey: str, pageurl: str, timeout: int = 90,
                          proxy: Optional[str] = None,
                          headless: Optional[bool] = None) -> dict:
    started = time.time()
    if headless is None:
        headless = os.environ.get("HEADLESS", "false").lower() == "true"
    try:
        from patchright.async_api import async_playwright
    except ImportError as e:
        return _fail(f"patchright_not_installed: {e}", started)
    display = None
    if not headless:
        try:
            display = _start_xvfb()
        except Exception:
            headless = True
    try:
        launch_opts = {"headless": headless, "args": _launch_args(),
                       "chromium_sandbox": False}
        if proxy:
            launch_opts["proxy"] = {"server": proxy}
        async with async_playwright() as p:
            browser = await p.chromium.launch(**launch_opts)
            try:
                context = await _new_context(browser)
                await context.add_init_script(STEALTH_JS)
                page = await context.new_page()
                await page.goto(pageurl, wait_until="domcontentloaded", timeout=30000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass
                await asyncio.sleep(2)
                iframe_sel = 'iframe[src*="google.com/recaptcha"], iframe[title*="recaptcha"]'
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
