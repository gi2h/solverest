FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PORT=8080
ENV DISPLAY=:99

RUN apt-get update && apt-get install -y \
    python3 python3-pip xvfb wget curl gnupg \
    libnss3 libatk-bridge2.0-0 libdrm2 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Install Patchright (disarankan PyPasser) dan Playwright sebagai fallback
RUN patchright install chromium || true
RUN playwright install chromium || true

COPY solver.py .

# Entrypoint: Jalankan Xvfb (layar virtual) lalu server
RUN echo '#!/bin/bash\nXvfb :99 -screen 0 1920x1080x24 & \nsleep 2\npython3 solver.py' > /app/start.sh && chmod +x /app/start.sh

EXPOSE 8080
CMD ["/app/start.sh"]
