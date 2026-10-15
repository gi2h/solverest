#!/bin/sh
# ═══════════════════════════════════════════════════════════════════════
# Fortress Captcha — Service Dispatcher
# Supports structure: services/<name>/ + workers/ at root
# ═══════════════════════════════════════════════════════════════════════
set -e

SERVICE="${SERVICE:-api-gateway}"
echo "[start] SERVICE=$SERVICE PORT=${PORT:-unset} PWD=$(pwd)"

# ── Run a FastAPI service from a directory ──────────────────────────
run_uvicorn() {
    dir="$1"; default_port="$2"
    port="${PORT:-$default_port}"
    cd "/app/$dir" || { echo "[start] FATAL: no dir /app/$dir" >&2; exit 1; }
    APP="main:app"
    [ -f "app/main.py" ] && APP="app.main:app"
    echo "[start] cd $dir → uvicorn $APP :$port"
    exec uvicorn "$APP" --host 0.0.0.0 --port "$port" --workers 1
}

# ── Dispatch ────────────────────────────────────────────────────────
case "$SERVICE" in
    api-gateway|gateway)
        run_uvicorn "services/api-gateway" 8080 ;;

    altcha|altcha-service|altcha-solver)
        run_uvicorn "services/altcha-service" 9005 ;;

    friendly|friendly-service|friendly-solver)
        run_uvicorn "services/friendly-service" 9006 ;;

    ocr|ocr-service|ocr-solver)
        run_uvicorn "services/ocr-service" 9003 ;;

    router|router-service)
        run_uvicorn "services/router-service" 9007 ;;

    cloak|cloak-service)
        run_uvicorn "services/cloak-service" 9001 ;;

    vision|vision-service|vision-solver)
        MODEL_DIR="${QWEN_LOCAL_DIR:-/data/models/qwen}"
        if [ ! -f "$MODEL_DIR/.download-complete" ]; then
            echo "[start] downloading Qwen model → $MODEL_DIR (first boot only)"
            python - <<'PYEOF' || echo "[start] WARN: model download failed, continuing"
import os, sys
from pathlib import Path
try:
    from huggingface_hub import snapshot_download
except ImportError:
    print("[dl] huggingface_hub not installed", file=sys.stderr); sys.exit(1)
dest = Path(os.environ.get("QWEN_LOCAL_DIR", "/data/models/qwen"))
dest.mkdir(parents=True, exist_ok=True)
repo = os.environ.get("QWEN_REPO", "Qwen/Qwen2-VL-2B-Instruct")
token = os.environ.get("HF_TOKEN") or None
print(f"[dl] {repo} → {dest}")
try:
    snapshot_download(repo_id=repo, local_dir=str(dest), token=token,
                      ignore_patterns=["*.msgpack","*.h5","*.ot","*.tflite","*.onnx"])
    (dest / ".download-complete").touch()
    print("[dl] OK")
except Exception as e:
    print(f"[dl] FAILED: {e}", file=sys.stderr); sys.exit(1)
PYEOF
        else
            echo "[start] model present at $MODEL_DIR"
        fi
        run_uvicorn "services/vision-service" 9002 ;;

    worker|image-worker|token-worker|cleanup-worker)
        cd /app
        echo "[start] starting worker (health shim on :${PORT:-8080})"
        exec python - <<'PYEOF'
import importlib, os, sys, threading
from http.server import HTTPServer, BaseHTTPRequestHandler

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok","role":"worker"}')
        else:
            self.send_response(404); self.end_headers()
    def log_message(self, *a): pass

def serve():
    port = int(os.environ.get("PORT", "8080"))
    HTTPServer(("0.0.0.0", port), H).serve_forever()

threading.Thread(target=serve, daemon=True).start()
print("[worker] health shim listening on", os.environ.get("PORT", "8080"), flush=True)

svc = os.environ.get("SERVICE", "worker")
try:
    if svc == "token-worker":
        from workers.token_worker import TokenWorker
        from workers.base_worker import run_worker
        run_worker(TokenWorker())
    elif svc == "cleanup-worker":
        import asyncio
        from workers.cleanup_worker import loop
        asyncio.run(loop())
    else:
        from workers.image_worker import ImageWorker
        from workers.base_worker import run_worker
        run_worker(ImageWorker())
except ImportError as e:
    print(f"[worker] import failed: {e}", file=sys.stderr)
    print("[worker] available files in /app/workers:", file=sys.stderr)
    import os as _os
    for f in _os.listdir("/app/workers"):
        print("  ", f, file=sys.stderr)
    sys.exit(1)
PYEOF
        ;;

    *)
        echo "[start] FATAL: unknown SERVICE='$SERVICE'" >&2
        echo "[start] valid: api-gateway | worker | altcha | friendly | vision | ocr | router | cloak" >&2
        exit 1 ;;
esac
