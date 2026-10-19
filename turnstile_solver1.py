import asyncio
from playwright.async_api import async_playwright
import urllib.parse
from typing import Optional

# KONFIGURASI (Sesuaikan dengan mkilik Anda)
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
HEADLESS = True

def parse_cookies(cookie_str: str, domain: str):
    """Mengubah string cookie 'a=1; b=2' menjadi format dict Playwright"""
    cookies = []
    if not cookie_str: return cookies
    for pair in cookie_str.split(";"):
        if "=" in pair:
            name, value = pair.strip().split("=", 1)
            cookies.append({
                "name": name,
                "value": value,
                "domain": domain,
                "path": "/"
            })
    return cookies

async def solve_turnstile(
    sitekey: str, 
    pageurl: str, 
    timeout: int = 90, 
    proxy: Optional[str] = None, 
    cookies: Optional[str] = None, 
    user_agent: Optional[str] = None
):
    async with async_playwright() as p:
        launch_args = {
            "headless": HEADLESS,
            "args": ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        }
        
        if proxy:
            launch_args["proxy"] = {"server": proxy}

        browser = await p.chromium.launch(**launch_args)
        
        # 1. Buat Context dengan User Agent (jika ada)
        context_args = {
            "viewport": {"width": 1280, "height": 800},
            "user_agent": user_agent or USER_AGENT
        }
        context = await browser.new_context(**context_args)

        # 2. SUNTIKKAN COOKIE (Jika script utama mengirimkannya)
        if cookies:
            parsed_domain = urllib.parse.urlparse(pageurl).netloc
            cookie_list = parse_cookies(cookies, parsed_domain)
            if cookie_list:
                await context.add_cookies(cookie_list)

        page = await context.new_page()
        
        try:
            # Logika asli Anda untuk navigasi dan pencarian iframe
            await page.goto(pageurl, wait_until="domcontentloaded", timeout=timeout * 1000)
            
            # Tunggu iframe Turnstile muncul
            iframe_selector = 'iframe[src*="challenges.cloudflare.com"]'
            await page.wait_for_selector(iframe_selector, timeout=15000)
            
            iframe_element = await page.query_selector(iframe_selector)
            iframe = await iframe_element.content_frame()
            
            # Logika klik
            checkbox = await iframe.query_selector('#checkbox')
            if checkbox:
                await checkbox.click()
            
            # Tunggu token
            for _ in range(timeout):
                token = await page.evaluate("""() => {
                    const input = document.querySelector('input[name="cf-turnstile-response"]');
                    return input ? input.value : null;
                }""")
                if token:
                    return {"token": token}
                await asyncio.sleep(1)
                
            return {"error": "Timeout waiting for token"}
            
        except Exception as e:
            return {"error": str(e)}
        finally:
            await context.close()
            await browser.close()
