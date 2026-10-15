#!/bin/sh
set -e
export PYTHONPATH="/solver:${PYTHONPATH:-}"
export PYTHONUNBUFFERED=1

PORT="${PORT:-8080}"
echo "[start] api-gateway booting on :$PORT"
echo "[start] flaresolverr target: ${FLARESOLVERR_URL:-http://127.0.0.1:8191}"

exec uvicorn app:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    --workers 1 \
    --log-level info
