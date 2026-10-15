# ═══════════════════════════════════════════════════════════════════════
# FORTRESS CAPTCHA — Universal Dockerfile
# Extracts arsip.zip at build time, installs deps based on $SERVICE.
# ═══════════════════════════════════════════════════════════════════════
FROM python:3.12-slim

ARG SERVICE=api-gateway
ENV SERVICE=${SERVICE} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app \
    DEBIAN_FRONTEND=noninteractive

# System deps: unzip + curl (healthcheck) + build essentials (wheel compile)
RUN apt-get update && apt-get install -y --no-install-recommends \
        unzip curl ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Extract the zip ─────────────────────────────────────────────────────
COPY arsip.zip /tmp/arsip.zip
RUN set -eux; \
    unzip -q /tmp/arsip.zip -d /tmp/x; \
    # Flatten if zip has a single top-level folder (fortress-captcha/...)
    count=$(ls /tmp/x | wc -l); \
    if [ "$count" = "1" ] && [ -d "/tmp/x/$(ls /tmp/x | head -1)" ]; then \
        inner="/tmp/x/$(ls /tmp/x | head -1)"; \
        mv "$inner"/* /app/ 2>/dev/null || true; \
        mv "$inner"/.[!.]* /app/ 2>/dev/null || true; \
    else \
        mv /tmp/x/* /app/ 2>/dev/null || true; \
        mv /tmp/x/.[!.]* /app/ 2>/dev/null || true; \
    fi; \
    rm -rf /tmp/x /tmp/arsip.zip; \
    ls -la /app

# ── Compat shims (OLD structure uses hyphenated dirs but underscore imports) ──
RUN set -eux; \
    cd /app/services 2>/dev/null || exit 0; \
    for hyphen in *-service *-solver; do \
        [ -d "$hyphen" ] || continue; \
        under=$(echo "$hyphen" | tr '-' '_'); \
        [ -e "$under" ] || ln -sf "$hyphen" "$under"; \
    done; \
    ls -la /app/services

# ── Install Python deps for the requested service ──────────────────────
RUN set -eux; \
    if [ -f "services/${SERVICE}/requirements.txt" ]; then \
        echo "→ installing services/${SERVICE}/requirements.txt"; \
        pip install --no-cache-dir -r "services/${SERVICE}/requirements.txt"; \
    elif [ -f "requirements.txt" ]; then \
        echo "→ installing root requirements.txt"; \
        pip install --no-cache-dir -r requirements.txt; \
    else \
        echo "ERROR: no requirements.txt found" >&2; exit 1; \
    fi

# ── Entrypoint dispatcher ──────────────────────────────────────────────
COPY start.sh /start.sh
RUN chmod +x /start.sh

# Default port (individual services override via env)
EXPOSE 8080

CMD ["/start.sh"]
