#!/bin/sh
# ═══════════════════════════════════════════════════════════════════════
# Fortress Captcha — start dispatcher
# Reads $SERVICE, runs the matching process. Supports both OLD and NEW
# service naming (e.g. altcha-solver | altcha-service | altcha).
# ═══════════════════════════════════════════════════════════════════════
set -e

SERVICE="${SERVICE:-api-gateway}"
echo "[start] SERVICE=$SERVICE  PORT=${PORT:-unset}  PWD=$(pwd)"

# ── Locate the service directory ───────────────────────────────────────
find_dir() {
    for d in \
        "services/$1" \
        "services/$1-service" \
        "services/$1-solver" \
        "services/${1%-service}" \
        "services/${1%-solver}" \
        "$1"
    do
        [ -d "$d" ] && { echo "$d"; return 0; }
    done
    return 1
}

# ── Locate uvicorn app entrypoint ──────────────────────────────────────
find_app() {
    if [ -f "app/main.py" ]; then echo "app.main:app"; return 0; fi
    if [ -f "main.py" ];     then echo "main:app";     return 0; fi
    if [ -f "server.py" ];   then echo "server:app";   return 0; fi
    return 1
}

SVC_DIR=$(find_dir "$SERVICE") || {
    echo "[start] FATAL: cannot find directory for SERVICE='$SERVICE'" >&2
    echo "[start] available under services/:" >&2
    ls -1 services/ 2>/dev/null >&2 || echo "  (none)" >&2
    exit 1
}
echo "[start] resolved dir: $SVC_DIR"
cd "$SVC_DIR"

# ── Dispatch ───────────────────────────────────────────────────────────
case "$SERVICE" in
    api-gateway|gateway)
        APP=$(find_app) || { echo "no app entry in $(pwd)" >&2; exit 1; }
        echo "[start] running uvicorn $APP on :${PORT:-8080}"
        exec uvicorn "$APP" --host 0.0.0.0 --port "${PORT:-8080}" --workers 1
        ;;

    worker|image-worker|token-worker)
        # Worker serves its own /health on $PORT and consumes streams.
        if [ -f "app/worker.py" ]; then
            exec python -m app.worker
        elif [ -f "worker.py" ]; then
            exec python worker.py
        elif [ -f "base_worker.py" ]; then
            exec python base_worker.py
        else
            echo "[start] no worker entry found in $(pwd)" >&2
            ls -la >&2
            exit 1
        fi
        ;;

    altcha|altcha-solver|altcha-service)
        APP=$(find_app) || { echo "no app entry" >&2; exit 1; }
        exec uvicorn "$APP" --host 0.0.0.0 --port "${PORT:-9005}"
        ;;

    friendly|friendly-solver|friendly-service)
        APP=$(find_app) || { echo "no app entry" >&2; exit 1; }
        exec uvicorn "$APP" --host 0.0.0.0 --port "${PORT:-9006}"
        ;;

    ocr|ocr-solver|ocr-service)
        APP=$(find_app) || { echo "no app entry" >&2; exit 1; }
        exec uvicorn "$APP" --host 0.0.0.0 --port "${PORT:-9003}"
        ;;

    cloak|cloak-solver|cloak-service)
        APP=$(find_app) || { echo "no app entry" >&2; exit 1; }
        exec uvicorn "$APP" --host 0.0.0.0 --port "${PORT:-9001}"
        ;;

    router|router-solver|router-service)
        APP=$(find_app) || { echo "no app entry" >&2; exit 1; }
        exec uvicorn "$APP" --host 0.0.0.0 --port "${PORT:-9007}"
        ;;

    vision|vision-solver|vision-service)
        MODEL_DIR="${QWEN_LOCAL_DIR:-/data/models/qwen}"
        if [ ! -f "$MODEL_DIR/.download-complete" ]; then
            echo "[start] model not found at $MODEL_DIR — downloading..."
            python - <<'PYEOF' || echo "[start] WARN: model download failed (service will start with model=null)"
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
print(f"[dl] repo={repo} dest={dest}")
try:
    snapshot_download(repo_id=repo, local_dir=str(dest), token=token,
                      ignore_patterns=["*.msgpack","*.h5","*.ot","*.tflite","*.onnx"])
    print("[dl] OK")
except Exception as e:
    print(f"[dl] FAILED: {e}", file=sys.stderr); sys.exit(1)
PYEOF
            touch "$MODEL_DIR/.download-complete" 2>/dev/null || true
        else
            echo "[start] model already present at $MODEL_DIR"
        fi
        APP=$(find_app) || { echo "no app entry" >&2; exit 1; }
        exec uvicorn "$APP" --host 0.0.0.0 --port "${PORT:-9002}"
        ;;

    *)
        echo "[start] FATAL: unknown SERVICE='$SERVICE'" >&2
        echo "[start] valid values: api-gateway worker altcha friendly ocr cloak router vision" >&2
        exit 1
        ;;
esac
