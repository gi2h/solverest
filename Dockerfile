# ═══════════════════════════════════════════════════════════════════════
# FORTRESS CAPTCHA — Universal Dockerfile
# Extracts arsip.zip, patches symlinks, installs deps, dispatches via start.sh
# ═══════════════════════════════════════════════════════════════════════
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app \
    DEBIAN_FRONTEND=noninteractive

# ── System packages ─────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        unzip curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Save root requirements as fallback ──────────────────────────────
COPY requirements.txt /app/requirements-root.txt

# ── Extract arsip.zip (flatten single top-level folder if present) ──
COPY arsip.zip /tmp/arsip.zip
RUN set -eux; \
    unzip -q /tmp/arsip.zip -d /tmp/x; \
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
    echo "--- /app ---"; ls -la /app; \
    echo "--- /app/services ---"; ls -la /app/services 2>/dev/null || echo "no services dir"; \
    echo "--- /app/workers ---"; ls -la /app/workers 2>/dev/null || echo "no workers dir"

# ── Create underscore symlinks for ALL hyphenated service dirs ──────
# IMPORTANT: uses *-* (not just *-service *-solver) so api-gateway,
# router-service, etc. all get their underscore aliases.
RUN set -eux; \
    cd /app/services; \
    for d in *-*; do \
        [ -d "$d" ] || continue; \
        under=$(echo "$d" | tr '-' '_'); \
        [ -e "$under" ] || ln -sf "$d" "$under"; \
    done; \
    echo "--- symlinks ---"; ls -la /app/services

# ── Install Python dependencies ─────────────────────────────────────
RUN set -eux; \
    if [ -f /app/requirements.txt ]; then \
        echo "→ installing /app/requirements.txt (from zip)"; \
        pip install --no-cache-dir -r /app/requirements.txt; \
    else \
        echo "→ installing /app/requirements-root.txt (from repo root)"; \
        pip install --no-cache-dir -r /app/requirements-root.txt; \
    fi

# ── Entrypoint ──────────────────────────────────────────────────────
COPY app.py /app/app.py
COPY start.sh /start.sh
RUN chmod +x /start.sh

EXPOSE 8080
CMD ["/start.sh"]
