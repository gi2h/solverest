FROM ghcr.io/flaresolverr/flaresolverr:latest

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/solver \
    FLARESOLVERR_ENABLED=1 \
    FLARESOLVERR_URL=http://127.0.0.1:8191

WORKDIR /solver

COPY requirements.txt /solver/requirements.txt
RUN pip install --no-cache-dir supervisor==4.2.5 \
 && pip install --no-cache-dir -r /solver/requirements.txt

COPY app.py /solver/app.py
COPY start.sh /solver/start.sh
COPY supervisord.conf /solver/supervisord.conf
RUN chmod +x /solver/start.sh

RUN mkdir -p /var/log/supervisor /var/run

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=5 \
  CMD curl -fsS "http://localhost:${PORT:-8080}/health" || exit 1

CMD ["/usr/local/bin/supervisord", "-c", "/solver/supervisord.conf"]
