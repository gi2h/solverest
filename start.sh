#!/bin/sh
# ═══════════════════════════════════════════════════════════════════════
# Fortress Captcha — Service Dispatcher
# Supports structure: services/<name>/ + workers/ at root
# ═══════════════════════════════════════════════════════════════════════
set -e

SERVICE="${SERVICE:-api-gateway}"
echo "[start] SERVICE=$SERVICE PORT=${PORT:-unset} PWD=$(pwd)"

# ── Run a FastAPI service from services/<name>/ ─────────────────────
run_uvicorn() {
    dir="$1"; default_port="$2"
    port="${PORT:-$default_port}"
    cd "/app/$dir" || { echo "[start] FATAL: no dir /app/$dir" >&2; exit 1; }
    APP="main:app"
    [ -f "app/main.py" ] && APP="app.main:app"
    echo "[start] cd $dir → uvicorn $APP :$port"
    exec uvicorn "$APP" --host 0.0.0.0 --port "$port" --workers 1
}

# ── Download Qwen model (first boot only, for vision service) ───────
ensure_model() {
    MODEL_DIR="${QWEN_LOCAL_DIR:-/data/models/qwen}"
    if [ -f "$MODEL_DIR/.download-complete" ]; then
        echo "[start] model present at $MODEL_DIR"
        return 0
    fi
    echo "[start] downloading Qwen model → $MODEL_DIR (first boot only)"
    python - <<'PYEOF' || echo "[start] WARN: model download failed, continuing with model=null"
import os, sys
from pathlib import Path
try:
    from huggingface_hub import snapshot_download
except ImportError:
    print("[dl] huggingface_hub not installed", file=sys.stderr); sys.exit(1)
dest = Path(os.environ.get("QWEN_LOCAL_DIR", "/data/models/qwen"))
dest.mkdir(parents=True, exist_ok=True)
repo = os.environ.get("QWEN_REPO", "Qwen/Qwen3-VL-2B-Instruct")
token = os.environ.get("HF_TOKEN") or None
print(f"[dl] repo={repo} → dest={dest}")
try:
    snapshot_download(
        repo_id=repo, local_dir=str(dest), token=token,
        ignore_patterns=["*.msgpack", "*.h5", "*.ot", "*.tflite", "*.onnx"],
    )
    (dest / ".download-complete").touch()
    print("[dl] OK")
except Exception as e:
    print(f"[dl] FAILED: {e}", file=sys.stderr); sys.exit(1)
PYEOF
}

# ── Worker runner (uses workers/ at /app root) ──────────────────────
run_worker() {
    cd /app
    WMOD=""
    case "$SERVICE" in
        worker|image-worker) WMOD="workers.image_worker" ;;
        token-worker)        WMOD="workers.token_worker" ;;
        cleanup-worker)      WMOD="workers.cleanup_worker" ;;
    esac
    echo "[start] worker module=$WMOD (health shim on :${PORT:-8080})"
    exec python - "$WMOD" <<'PYEOF'
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
print(f"[worker] health shim on :{os.environ.get('PORT', '8080')}", flush=True)

module_name = sys.argv[1]
try:
    mod = importlib.import_module(module_name)
except ImportError as e:
    print(f"[worker] import failed: {e}", file=sys.stderr)
    import os as _os
    print("[worker] contents of /app/workers:", file=sys.stderr)
    for f in _os.listdir("/app/workers"):
        print("  ", f, file=sys.stderr)
    sys.exit(1)

if hasattr(mod, "ImageWorker") and hasattr(mod, "run_worker"):
    mod.run_worker(mod.ImageWorker())
elif hasattr(mod, "TokenWorker") and hasattr(mod, "run_worker"):
    mod.run_worker(mod.TokenWorker())
elif hasattr(mod, "loop"):
    import asyncio; asyncio.run(mod.loop())
elif hasattr(mod, "main"):
    mod.main()
else:
    print(f"[worker] no valid entry in {module_name}", file=sys.stderr)
    print(f"[worker] module attrs: {dir(mod)}", file=sys.stderr)
    sys.exit(1)
PYEOF
}

# ── Dispatch by SERVICE ─────────────────────────────────────────────
case "$SERVICE" in
    api-gateway|gateway)
        run_uvicorn "services/api-gateway" 8080
        ;;

    altcha|altcha-service|altcha-solver)
        run_uvicorn "services/altcha-service" 9005
        ;;

    friendly|friendly-service|friendly-solver)
        run_uvicorn "services/friendly-service" 9006
        ;;

    ocr|ocr-service|ocr-solver)
        run_uvicorn "services/ocr-service" 9003
        ;;

    router|router-service)
        run_uvicorn "services/router-service" 9007
        ;;

    cloak|cloak-service)
        run_uvicorn "services/cloak-service" 9001
        ;;

    vision|vision-service|vision-solver)
        ensure_model
        run_uvicorn "services/vision-service" 9002
        ;;

    worker|image-worker|token-worker|cleanup-worker)
        run_worker
        ;;

    *)
        echo "[start] FATAL: unknown SERVICE='$SERVICE'" >&2
        echo "[start] valid values:" >&2
        echo "  api-gateway | worker | altcha | friendly | vision | ocr | router | cloak" >&2
        exit 1
        ;;
esac
