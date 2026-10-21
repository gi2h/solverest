#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Turnstile/ReCaptcha solver functions (Compatible with app.py).
"""

import asyncio
import os
import time
import logging
from typing import Optional, Dict
from urllib.parse import urlparse

from patchright.async_api import async_playwright

log = logging.getLogger("solver")

HEADLESS = os.getenv("SOLVER_HEADLESS", "false").lower() == "true"
NAV_TIMEOUT_MS = int(os.getenv("SOLVER_NAV_TIMEOUT_MS", "45000"))
SOLVE_TIMEOUT_S = int(os.getenv("SOLVER_SOLVE_TIMEOUT_S", "120"))

STEALTH_JS = r"""
(() => {
  try {
    if (typeof window.PFVisitCheck !== 'undefined') return;
  } catch (e) {}
  window.PFVisitCheck = {
    ok: true, isOk: () => true, isFine: () => true, check: () => true,
    verify: () => true, passed: true, status: 'ok', result: true,
    isBot: false, isHuman: true, adblock: false, adBlockDetected: false,
    blockers: [], detected: [], visitCheck: true, visit_check: true,
    checkVisit: () => true, verifyVisit: () => true, verify: async () => true,
    run: async () => true, init: () => true, report: () => true, sendBeacon: () => true,
  };
  const _origCreateElement = document.createElement.bind(document);
  document.createElement = function (tag, ...rest) {
    const el = _origCreateElement(tag, ...rest);
    if (typeof tag === 'string' && /bait|ad|ads|advert|banner|sponsor/i.test(tag)) {
      try { el.offsetHeight = 1; el.offsetWidth = 1; } catch (_) {}
    }
    return el;
  };
  try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); } catch (_) {}
})();
"""

def _proxy_opts(proxy: str) -> Dict[str, str]:
    """Chromium membuang user:pass@, Playwright butuh terpisah."""
    if not proxy:
        return {}
    if "://" not in proxy:
        proxy = "http://" + proxy
    u = urlparse(proxy)
    if not u.hostname or not u.port:
        return {"server": proxy}
    opts = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        opts["username"] = u.username
    if u.password:
        opts["password"] = u.password
    return opts

async def _launch_browser(playwright, proxy: Optional[str]):
    launch_opts = {
        "headless": HEADLESS,
        "args": [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
        ],
    }

    if proxy:
        opts = _proxy_opts(proxy)
        if opts:
            launch_opts["proxy"] = opts
            safe = {**opts}
            if "password" in safe:
                safe["password"] = "***"
            log.info("launch proxy=%s", safe)

    return await playwright.chromium.launch(**launch_opts)


async def solve_turnstile(sitekey: str, pageurl: str, proxy: Optional[str] = None) -> Optional[str]:
    """Fungsi yang dipanggil app.py untuk Turnstile."""
    async with async_playwright() as pw:
        browser = await _launch_browser(pw, proxy)
        try:
            context = await browser.new_context(
                user_agent=("Mozilla/5.0 (Linux; Android 13; 22120RN86G Build/TQ3A.230901.001) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.7204.63 Mobile Safari/537.36"),
                viewport={"width": 412, "height": 915},
                locale="en-US",
            )
            await context.add_init_script(STEALTH_JS)
            page = await context.new_page()

            try:
                await page.goto("https://api.ipify.org", wait_until="domcontentloaded", timeout=15000)
                egress = (await page.inner_text("body")).strip()
                log.info("turnstile: egress ip = %s", egress)
            except Exception:
                pass

            await page.goto(pageurl, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)

            deadline = time.time() + SOLVE_TIMEOUT_S
            clicked = False

            while time.time() < deadline:
                token = await page.evaluate(
                    """() => {
                        const el = document.querySelector('input[name="cf-turnstile-response"]');
                        return el ? el.value : null;
                    }"""
                )
                if token:
                    return token

                if not clicked:
                    for sel in ("iframe[src*='challenges.cloudflare.com']", "div.cf-turnstile", "[data-sitekey]"):
                        try:
                            el = await page.query_selector(sel)
                            if el:
                                box = await el.bounding_box()
                                if box:
                                    await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                                    clicked = True
                                    break
                        except Exception:
                            continue
                await asyncio.sleep(1.0)

            return None
        finally:
            try:
                await browser.close()
            except Exception:
                pass


async def solve_recaptcha(sitekey: str, pageurl: str, proxy: Optional[str] = None) -> Optional[str]:
    """Fungsi yang dipanggil app.py untuk ReCaptcha."""
    async with async_playwright() as pw:
        browser = await _launch_browser(pw, proxy)
        try:
            context = await browser.new_context(locale="en-US")
            await context.add_init_script(STEALTH_JS)
            page = await context.new_page()
            await page.goto(pageurl, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)

            deadline = time.time() + SOLVE_TIMEOUT_S
            while time.time() < deadline:
                tok = await page.evaluate(
                    """() => {
                        const el = document.querySelector('textarea[name="g-recaptcha-response"]');
                        return el ? el.value : null;
                    }"""
                )
                if tok:
                    return tok
                try:
                    frame = page.frame_locator("iframe[src*='recaptcha/api2/anchor']")
                    await frame.locator("#recaptcha-anchor").click(timeout=2000)
                except Exception:
                    pass
                await asyncio.sleep(1.0)
            return None
        finally:
            try:
                await browser.close()
            except Exception:
                pass


async def get_cloudflare_cookie(pageurl: str, proxy: Optional[str] = None) -> Optional[dict]:
    """Fungsi yang dipanggil app.py untuk ambil Cookie CF (cf_clearance)."""
    async with async_playwright() as pw:
        browser = await _launch_browser(pw, proxy)
        try:
            context = await browser.new_context()
            await context.add_init_script(STEALTH_JS)
            page = await context.new_page()
            await page.goto(pageurl, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            
            # Beri waktu CF menyelesaikan tantangan
            await asyncio.sleep(5)
            
            cookies = await context.cookies()
            return {c['name']: c['value'] for c in cookies}
        finally:
            try:
                await browser.close()
            except Exception:
                pass
