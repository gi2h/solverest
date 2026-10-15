FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/solver \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /solver

# Chromium system libs + Xvfb (virtual display) + utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
        libnss3 libnspr4 \
        libatk1.0-0 libatk-bridge2.0-0 \
        libcups2 libdrm2 \
        libxkbcommon0 libxcomposite1 \
        libxdamage1 libxfixes3 libxrandr2 \
        libgbm1 libpango-1.0-0 \
        libcairo2 libasound2 \
        libatspi2.0-0 \
        libx11-xcb1 libxcb-dri3-0 \
        fonts-liberation fonts-noto-color-emoji \
        xvfb x11-utils \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# Python deps + Patchright Chromium
COPY requirements.txt /solver/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r /solver/requirements.txt \
 && patchright install chromium \
 && chmod -R 777 /ms-playwright

# App files
COPY app.py turnstile_solver1.py /solver/
COPY start.sh /solver/start.sh
RUN chmod +x /solver/start.sh

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=45s --retries=5 \
  CMD curl -fsS "http://localhost:${PORT:-8080}/health" || exit 1

CMD ["/solver/start.sh"]
