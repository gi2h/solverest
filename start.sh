#!/bin/sh
set -e
export PYTHONPATH="/solver:${PYTHONPATH:-}"
export PYTHONUNBUFFERED=1
export PLAYWRIGHT_BROWSERS_PATH="/ms-playwright"

PORT="${PORT:-8080}"
echo "[start] Fortress Captcha v4.1 (patchright + xvfb) on :$PORT"

exec uvicorn app:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    --workers 1 \
    --log-level info
