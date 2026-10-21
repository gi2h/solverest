#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Standar Turnstile Solver (Clean & Stable Navigation)
"""

import asyncio
import os
import time
import logging
import traceback
from typing import Optional, Dict
from urllib.parse import urlparse

from patchright.async_api import async_playwright

log = logging.getLogger("solver")

HEADLESS = os.getenv("SOLVER_HEADLESS", "false").lower() == "true"
NAV_TIMEOUT_MS = int(os.getenv("SOLVER_NAV_TIMEOUT_MS", "45000"))

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
  try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); } catch (_) {}
})();
"""

def _proxy_opts(proxy: str) -> Dict[str, str]:
    """Memisahkan kredensial proxy agar dikenali Playwright"""
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

async def _launch_browser(playwright):
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
    return await playwright.chromium.launch(**launch_opts)


async def solve_turnstile(sitekey: str, pageurl: str, proxy: Optional[str] = None, timeout: int = 60, **kwargs) -> Optional[str]:
    async with async_playwright() as pw:
        browser = await _launch_browser(pw)
        try:
            context_opts = {
                "user_agent": ("Mozilla/5.0 (Linux; Android 13; 22120RN86G Build/TQ3A.230901.001) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.7204.63 Mobile Safari/537.36"),
                "viewport": {"width": 412, "height": 915},
                "locale": "en-US",
            }
            
            # Proxy dimasukkan ke context
            if proxy:
                opts = _proxy_opts(proxy)
                if opts:
                    context_opts["proxy"] = opts
                    log.info("context proxy=%s", opts.get("server"))

            context = await browser.new_context(**context_opts)
            await context.add_init_script(STEALTH_JS)
            page = await context.new_page()

            # Navigasi normal
            try:
                log.info(f"Navigating to {pageurl}")
                await page.goto(pageurl, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            except Exception as e:
                log.warning(f"Navigation issue: {str(e)[:50]}")

            deadline = time.time() + timeout
            clicked = False

            # Loop standar: cari token
            while time.time() < deadline:
                try:
                    token = await page.evaluate(
                        """() => {
                            const el = document.querySelector('input[name="cf-turnstile-response"]');
                            return el ? el.value : null;
                        }"""
                    )
                    if token and len(token) > 20:
                        log.info(f"turnstile: token acquired len={len(token)}")
                        return token

                    # Klik iframe jika belum
                    if not clicked:
                        for sel in ("iframe[src*='challenges.cloudflare.com']", "div.cf-turnstile", "[data-sitekey]"):
                            try:
                                el = await page.query_selector(sel)
                                if el:
                                    box = await el.bounding_box()
                                    if box:
                                        await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                                        clicked = True
                                        log.info("turnstile: widget clicked")
                                        break
                            except Exception:
                                continue
                except Exception:
                    pass
                await asyncio.sleep(1.0)

            log.warning("turnstile: timeout, no token")
            return None

        except Exception:
            log.error(traceback.format_exc())
            return None
        finally:
            try:
                await browser.close()
            except Exception:
                pass

# Fungsi placeholder untuk memenuhi import app.py
async def solve_recaptcha(sitekey: str, pageurl: str, proxy: Optional[str] = None, timeout: int = 60, **kwargs) -> Optional[str]:
    return None

async def get_cloudflare_cookie(pageurl: str, proxy: Optional[str] = None, timeout: int = 60, **kwargs) -> Optional[dict]:
    return None
