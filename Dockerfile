FROM node:20-slim

# Install dependensi Linux untuk menjalankan Chrome/Puppeteer
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    ca-certificates \
    xvfb \
    libnss3 \
    libatk-bridge2.0-0 \
    libcups2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /solver

# Copy package.json dan install dependencies
COPY package*.json ./
RUN npm install

# Copy semua file source code
COPY . .

# Port default (sesuai app.js)
EXPOSE 8080
ENV PORT=8080

# Jalankan Node.js
CMD ["node", "app.js"]
