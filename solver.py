import asyncio
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# --- Import Engine Patchright (Wajib untuk PyPasser Turnstile) ---
try:
    from patchright.async_api import async_playwright
    USING_PATCHRIGHT = True
except ImportError:
    from playwright.async_api import async_playwright
    USING_PATCHRIGHT = False

# --- Import API Resmi PyPasser ---
try:
    from pypasser.captcha import turnstile
    HAS_PYPASSER = True
except ImportError:
    HAS_PYPASSER = False

# =====================================================================
# KONFIGURASI
# =====================================================================
PORT = int(os.getenv("PORT", 8080))
HOST = "0.0.0.0"

browser_instance = None
playwright_instance = None

# Penyimpanan Task di Memori
TASKS: Dict[str, Dict[str, Any]] = {}

# =====================================================================
# LIFESPAN (Startup & Shutdown)
# =====================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global browser_instance, playwright_instance
    print(f"[*] Memulai Browser (Patchright: {USING_PATCHRIGHT}, PyPasser: {HAS_PYPASSER})...")
    
    playwright_instance = await async_playwright().start()
    
    # Jalankan Chromium HEADFUL (wajib untuk Cloudflare)
    browser_instance = await playwright_instance.chromium.launch(
        headless=False, 
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--start-maximized",
        ]
    )
    print("[*] Browser berhasil dijalankan.")
    yield
    
    print("[*] Mematikan Browser...")
    if browser_instance:
        await browser_instance.close()
    if playwright_instance:
        await playwright_instance.stop()

app = FastAPI(lifespan=lifespan)

# =====================================================================
# MODEL REQUEST / RESPONSE
# =====================================================================
class SolveRequest(BaseModel):
    pageurl: str
    sitekey: Optional[str] = None

# =====================================================================
# CORE SOLVER LOGIC (API RESMI PYPASSER)
# =====================================================================
async def solve_turnstile_core(pageurl: str, sitekey: Optional[str] = None) -> Optional[str]:
    if not browser_instance:
        raise Exception("Browser belum siap.")
    
    if not HAS_PYPASSER:
        raise Exception("PyPasser tidak terinstal.")

    context = await browser_instance.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        timezone_id="America/New_York",
    )
    
    # Sembunyikan Webdriver
    await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    
    page = await context.new_page()

    try:
        print(f"[*] Navigasi ke: {pageurl}")
        await page.goto(pageurl, wait_until="domcontentloaded", timeout=45000)

        # Panggil API PyPasser untuk Turnstile
        print("[*] Menjalankan PyPasser turnstile.asolve(page)...")
        # Parameter default: backend="auto", attempts=3, timeout=30
        result = await turnstile.asolve(page)
        
        # Periksa hasil sesuai dokumentasi (properti is_solved dan response_token)
        if getattr(result, 'is_solved', False):
            token = getattr(result, 'response_token', None)
            if token:
                print(f"[+] Token via PyPasser! (len={len(token)})")
                return token
            else:
                print("[-] PyPasser melaporkan is_solved=True tetapi response_token kosong.")
                return None
        else:
            print("[-] PyPasser gagal menyelesaikan Turnstile (is_solved=False).")
            return None

    except Exception as e:
        print(f"[!] Error saat solve: {str(e)}")
        return None
    finally:
        await page.close()
        await context.close()

# =====================================================================
# BACKGROUND TASK RUNNER
# =====================================================================
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
# ENDPOINT API (KOMPATIBEL GI7XX)
# =====================================================================
@app.post("/v1/solve/turnstile")
async def api_submit(req: SolveRequest):
    task_id = str(uuid.uuid4())
    TASKS[task_id] = {"status": "processing", "token": None, "error": None}
    asyncio.create_task(run_solve_task(task_id, req.pageurl, req.sitekey))
    print(f"[*] Submit Task ID: {task_id}")
    # Format respons submit: {"id": "..."}
    return {"id": task_id}

@app.get("/v1/solve/result/{task_id}")
async def api_get_result(task_id: str):
    if task_id not in TASKS:
        raise HTTPException(status_code=404, detail="Task ID not found")
    
    task = TASKS[task_id]
    if task["status"] == "success":
        # Format respons sukses: {"status": "ready", "solution": "..."}
        return {"status": "ready", "solution": task["token"]}
    elif task["status"] == "error":
        # Format respons error: {"status": "error", "error": "..."}
        return {"status": "error", "error": task["error"]}
    else:
        # Format respons proses: {"status": "processing"}
        return {"status": "processing"}

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "browser_ready": browser_instance is not None,
        "patchright": USING_PATCHRIGHT,
        "pypasser": HAS_PYPASSER
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
