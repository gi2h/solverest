#!/bin/sh
# ═══════════════════════════════════════════════════════════════════════
# FORTRESS CAPTCHA - universal service dispatcher
# Works with: api-gateway, vision-solver, altcha, friendly, worker, dll
# flaresolverr tidak pakai ini - dia punya CMD sendiri di Dockerfile
# ═══════════════════════════════════════════════════════════════════════
set -e

SERVICE="${SERVICE:-api-gateway}"
echo "[start] SERVICE=$SERVICE PORT=${PORT:-unset} PWD=$(pwd)"

# Ensure /app is on PYTHONPATH for app.py and shared modules
export PYTHONPATH="/app:${PYTHONPATH:-}"
export PYTHONUNBUFFERED=1

case "$SERVICE" in

    # ═══════════════════════════════════════════════════════════════════
    # API GATEWAY - v3.0 self-contained app.py
    # ═══════════════════════════════════════════════════════════════════
    api-gateway|gateway)
        if [ -f "/app/app.py" ]; then
            echo "[start] running v3.0 app.py (uvicorn app:app) on :${PORT:-8080}"
            exec uvicorn app:app \
                --host 0.0.0.0 \
                --port "${PORT:-8080}" \
                --workers 1 \
                --log-level info
        fi
        # Fallback untuk versi lama
        echo "[start] app.py not found, falling back to legacy services/api-gateway"
        cd /app/services/api-gateway
        exec uvicorn main:app \
            --host 0.0.0.0 \
            --port "${PORT:-8080}" \
            --workers 1
        ;;

    # ═══════════════════════════════════════════════════════════════════
    # VISION SOLVER - Qwen VL via vision-service microservice
    # ═══════════════════════════════════════════════════════════════════
    vision|vision-service|vision-solver)
        # Unpack arsip.zip kalau belum
        if [ ! -d /app/services/vision-service ]; then
            echo "[start] unpacking arsip.zip"
            mkdir -p /app/_unpack
            unzip -q /tmp/arsip.zip -d /app/_unpack
            count=$(ls /app/_unpack | wc -l)
            if [ "$count" = "1" ] && [ -d "/app/_unpack/$(ls /app/_unpack | head -1)" ]; then
                inner="/app/_unpack/$(ls /app/_unpack | head -1)"
                mv "$inner"/* /app/ 2>/dev/null || true
                mv "$inner"/.[!.]* /app/ 2>/dev/null || true
            else
                mv /app/_unpack/* /app/ 2>/dev/null || true
                mv /app/_unpack/.[!.]* /app/ 2>/dev/null || true
            fi
            rm -rf /app/_unpack
        fi

        # Download Qwen model (first boot only)
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
print(f"[dl] repo={repo} → dest={dest}", flush=True)
try:
    snapshot_download(
        repo_id=repo, local_dir=str(dest),
        token=os.environ.get("HF_TOKEN") or None,
        ignore_patterns=["*.msgpack", "*.h5", "*.ot", "*.tflite", "*.onnx"],
    )
    (dest / ".download-complete").touch()
    print("[dl] OK", flush=True)
except Exception as e:
    print(f"[dl] FAILED: {e}", file=sys.stderr); sys.exit(1)
PYEOF
        else
            echo "[start] model already present at $MODEL_DIR"
        fi

        cd /app/services/vision-service
        exec uvicorn main:app \
            --host 0.0.0.0 \
            --port "${PORT:-9002}" \
            --workers 1
        ;;

    # ═══════════════════════════════════════════════════════════════════
    # LEGACY MICROSERVICES (from arsip.zip)
    # ═══════════════════════════════════════════════════════════════════
    altcha|altcha-service|altcha-solver)
        cd /app/services/altcha-service
        exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-9005}" --workers 1
        ;;

    friendly|friendly-service|friendly-solver)
        cd /app/services/friendly-service
        exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-9006}" --workers 1
        ;;

    ocr|ocr-service|ocr-solver)
        cd /app/services/ocr-service
        exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-9003}" --workers 1
        ;;

    cloak|cloak-service)
        cd /app/services/cloak-service
        exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-9001}" --workers 1
        ;;

    router|router-service)
        cd /app/services/router-service
        exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-9007}" --workers 1
        ;;

    # ═══════════════════════════════════════════════════════════════════
    # WORKERS (from arsip.zip workers/ dir)
    # ═══════════════════════════════════════════════════════════════════
    worker|image-worker)
        cd /app
        exec python -m workers.image_worker
        ;;

    token-worker)
        cd /app
        exec python -m workers.token_worker
        ;;

    cleanup-worker)
        cd /app
        exec python -m workers.cleanup_worker
        ;;

    # ═══════════════════════════════════════════════════════════════════
    # UNKNOWN
    # ═══════════════════════════════════════════════════════════════════
    *)
        echo "[start] FATAL: unknown SERVICE='$SERVICE'" >&2
        echo "[start] valid values:" >&2
        echo "  api-gateway | vision | altcha | friendly | ocr | cloak | router" >&2
        echo "  worker | token-worker | cleanup-worker" >&2
        exit 1
        ;;
esac
