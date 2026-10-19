from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
import uvicorn
import uuid
import asyncio
from turnstile_solver1 import turnstile_solver # Pastikan nama file import sesuai

app = FastAPI(title="Fortress Captcha API v4.2")

class BrowserRequest(BaseModel):
    sitekey: str
    pageurl: str
    action: Optional[str] = None
    proxy: Optional[str] = None
    cookies: Optional[str] = None       # <-- BARU: Menerima string cookie
    user_agent: Optional[str] = None    # <-- BARU: Menerima User Agent

class TaskResponse(BaseModel):
    id: str
    status: str

@app.post("/v1/solve/turnstile", response_model=TaskResponse)
async def create_task(req: BrowserRequest):
    task_id = str(uuid.uuid4())
    # Simulasi penyimpanan task (di produksi gunakan Redis/DB)
    tasks_db[task_id] = {"status": "pending", "result": None}
    
    # Jalankan solver di background
    asyncio.create_task(run_solver_task(task_id, req))
    
    return {"id": task_id, "status": "pending"}

tasks_db = {}

async def run_solver_task(task_id: str, req: BrowserRequest):
    try:
        # Panggil fungsi solver dengan parameter cookie
        res = await solve_turnstile(
            sitekey=req.sitekey,
            pageurl=req.pageurl,
            proxy=req.proxy,
            cookies=req.cookies,
            user_agent=req.user_agent
        )
        if res.get("error"):
            tasks_db[task_id] = {"status": "failed", "error": res["error"]}
        else:
            tasks_db[task_id] = {"status": "done", "result": res["token"]}
    except Exception as e:
        tasks_db[task_id] = {"status": "failed", "error": str(e)}

@app.get("/v1/tasks/{task_id}/wait")
async def wait_task(task_id: str, timeout: int = 100):
    start = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start < timeout:
        if task_id in tasks_db:
            task = tasks_db[task_id]
            if task["status"] in ["done", "failed"]:
                return task
        await asyncio.sleep(1)
    raise HTTPException(status_code=408, detail="Timeout")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
