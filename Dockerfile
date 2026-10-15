# ═══════════════════════════════════════════════════════════════════════
# FORTRESS CAPTCHA - All-in-one
# Base: FlareSolverr image (Python 3.11 + Chromium + chromedriver)
# Adds: api-gateway (app.py) via supervisord
# Runs: flaresolverr on 127.0.0.1:8191 + uvicorn on 0.0.0.0:$PORT
# ═══════════════════════════════════════════════════════════════════════
FROM ghcr.io/flaresolverr/flaresolverr:latest

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/solver \
    FLARESOLVERR_ENABLED=1 \
    FLARESOLVERR_URL=http://127.0.0.1:8191 \
    DEBIAN_FRONTEND=noninteractive

# ── System packages: supervisor + utilities ─────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        supervisor unzip curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ── Our app in /solver (FlareSolverr stays in /app) ─────────────────────
WORKDIR /solver

# Python deps for app.py
COPY requirements.txt /solver/requirements.txt
RUN pip install --no-cache-dir -r /solver/requirements.txt

# App files
COPY app.py /solver/app.py
COPY start.sh /solver/start.sh
COPY supervisord.conf /solver/supervisord.conf
RUN chmod +x /solver/start.sh

# arsip.zip optional (untuk vision-solver legacy, kalau ada)
COPY arsip.zip /tmp/arsip.zip

# ── Supervisor configuration ────────────────────────────────────────────
RUN mkdir -p /var/log/supervisor /var/run

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=5 \
  CMD curl -fsS "http://localhost:${PORT:-8080}/health" || exit 1

CMD ["/usr/bin/supervisord", "-c", "/solver/supervisord.conf"]
