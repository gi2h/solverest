import asyncio
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

try:
    from patchright.async_api import async_playwright
    USING_PATCHRIGHT = True
except ImportError:
    from playwright.async_api import async_playwright
    USING_PATCHRIGHT = False

try:
    from pypasser.captcha import turnstile
    HAS_PYPASSER = True
except ImportError:
    HAS_PYPASSER = False

PORT = int(os.getenv("PORT", 8080))
HOST = "0.0.0.0"

browser_instance = None
playwright_instance = None
TASKS: Dict[str, Dict[str, Any]] = {}

@asynccontextmanager
async def lifespan(app: FastAPI):
    global browser_instance, playwright_instance
    print(f"[*] Memulai Browser (Patchright: {USING_PATCHRIGHT}, PyPasser: {HAS_PYPASSER})...")
    playwright_instance = await async_playwright().start()
    browser_instance = await playwright_instance.chromium.launch(
        headless=False, 
        args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"]
    )
    print("[*] Browser siap.")
    yield
    if browser_instance: await browser_instance.close()
    if playwright_instance: await playwright_instance.stop()

app = FastAPI(lifespan=lifespan)

class SolveRequest(BaseModel):
    sitekey: Optional[str] = None
    pageurl: str

# =====================================================================
# CORE SOLVER (PYPASSER + FALLBACK)
# =====================================================================
async def solve_turnstile_core(pageurl: str, sitekey: Optional[str] = None) -> Optional[str]:
    if not browser_instance: raise Exception("Browser belum siap.")

    context = await browser_instance.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        viewport={"width": 1920, "height": 1080}, locale="en-US", timezone_id="America/New_York"
    )
    await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    page = await context.new_page()

    try:
        print(f"[*] Navigasi ke: {pageurl}")
        await page.goto(pageurl, wait_until="domcontentloaded", timeout=45000)

        if HAS_PYPASSER:
            print("[*] Menjalankan PyPasser...")
            try:
                result = await turnstile.asolve(page)
                if getattr(result, 'is_solved', False):
                    token = getattr(result, 'response_token', None)
                    if token: return token
            except: pass

        print("[*] Fallback manual...")
        for _ in range(3): # Coba 3x
            # Cek input hidden
            token_input = await page.query_selector('input[name="cf-turnstile-response"]')
            if token_input:
                token = await token_input.get_attribute("value")
                if token and len(token) > 20: return token
            
            # Cek iframe & klik jika perlu
            for frame in page.frames:
                if "challenges.cloudflare.com" in frame.url:
                    try:
                        token_elem = await frame.wait_for_selector('input[name="cf-turnstile-response"]', timeout=3000)
                        token = await token_elem.get_attribute("value")
                        if token and len(token) > 20: return token
                        checkbox = await frame.query_selector('input[type="checkbox"]')
                        if checkbox: await checkbox.click()
                    except: pass
            await page.wait_for_timeout(3000)
        
        return None
    except Exception as e:
        print(f"[!] Error: {str(e)}")
        return None
    finally:
        await page.close()
        await context.close()

async def run_solve_task(task_id: str, pageurl: str, sitekey: Optional[str]):
    try:
        token = await solve_turnstile_core(pageurl, sitekey)
        if token:
            TASKS[task_id].update({"status": "success", "token": token})
        else:
            TASKS[task_id].update({"status": "error", "error": "Gagal mendapatkan token."})
    except Exception as e:
        TASKS[task_id].update({"status": "error", "error": str(e)})

# =====================================================================
# ENDPOINT API (FORMAT GI7XX)
# =====================================================================
@app.post("/v1/solve/turnstile")
async def api_submit(req: SolveRequest):
    task_id = str(uuid.uuid4())
    TASKS[task_id] = {"status": "processing", "token": None, "error": None}
    asyncio.create_task(run_solve_task(task_id, req.pageurl, req.sitekey))
    print(f"[*] Submit Task ID: {task_id}")
    # FORMAT GI7XX: {"id": "..."}
    return {"id": task_id}

@app.get("/v1/solve/result/{task_id}")
async def api_get_result(task_id: str):
    if task_id not in TASKS:
        raise HTTPException(status_code=404, detail="Task ID not found")
    
    task = TASKS[task_id]
    if task["status"] == "success":
        # FORMAT GI7XX: {"status": "ready", "solution": "..."}
        return {"status": "ready", "solution": task["token"]}
    elif task["status"] == "error":
        # FORMAT GI7XX: {"status": "error", "error": "..."}
        return {"status": "error", "error": task["error"]}
    else:
        # FORMAT GI7XX: {"status": "processing"}
        return {"status": "processing"}

@app.get("/health")
async def health(): return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
