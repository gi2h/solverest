# ═══════════════════════════════════════════════════════════════════════
# FORTRESS CAPTCHA - All-in-one (fix: non-root base image)
# ═══════════════════════════════════════════════════════════════════════
FROM ghcr.io/flaresolverr/flaresolverr:latest

# FlareSolverr image runs as non-root user by default.
# Switch to root to allow COPY + chmod + supervisord setup.
USER root

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/solver \
    FLARESOLVERR_ENABLED=1 \
    FLARESOLVERR_URL=http://127.0.0.1:8191

WORKDIR /solver

# Python deps for api-gateway + supervisor (no apt-get, pip only)
COPY requirements.txt /solver/requirements.txt
RUN pip install --no-cache-dir supervisor==4.2.5 \
 && pip install --no-cache-dir -r /solver/requirements.txt

# App files
COPY app.py /solver/app.py
COPY start.sh /solver/start.sh
COPY supervisord.conf /solver/supervisord.conf

# Ensure start.sh is executable (works because we're root now)
RUN chmod 755 /solver/start.sh \
 && mkdir -p /var/log/supervisor /var/run

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=5 \
  CMD curl -fsS "http://localhost:${PORT:-8080}/health" || exit 1

CMD ["/usr/local/bin/supervisord", "-c", "/solver/supervisord.conf"]
