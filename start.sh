#!/bin/sh
# ═══════════════════════════════════════════════════════════════════════
# Fortress Captcha — dispatcher
# api-gateway: runs self-contained app.py (no Redis, no 2captcha)
# vision-solver: unpack arsip.zip and run Qwen VL service
# ═══════════════════════════════════════════════════════════════════════
set -e

SERVICE="${SERVICE:-api-gateway}"
echo "[start] SERVICE=$SERVICE PORT=${PORT:-unset} PWD=$(pwd)"

case "$SERVICE" in
    api-gateway|gateway)
        # Self-contained app — no Redis, no unpack needed
        exec uvicorn app:app \
            --host 0.0.0.0 \
            --port "${PORT:-8080}" \
            --workers 1 \
            --log-level info
        ;;

    vision|vision-service|vision-solver)
        # Unpack arsip.zip if not yet unpacked
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
            fi
            rm -rf /app/_unpack
        fi

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
try:
    snapshot_download(repo_id=repo, local_dir=str(dest),
                      token=os.environ.get("HF_TOKEN") or None,
                      ignore_patterns=["*.msgpack","*.h5","*.ot","*.tflite","*.onnx"])
    (dest / ".download-complete").touch()
    print("[dl] OK")
except Exception as e:
    print(f"[dl] FAILED: {e}", file=sys.stderr); sys.exit(1)
PYEOF
        else
            echo "[start] model present at $MODEL_DIR"
        fi

        cd /app/services/vision-service
        exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-9002}" --workers 1
        ;;

    *)
        echo "[start] FATAL: unknown SERVICE='$SERVICE'" >&2
        echo "[start] valid: api-gateway | vision" >&2
        exit 1
        ;;
esac
