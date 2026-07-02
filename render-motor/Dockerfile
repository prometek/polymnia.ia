# POC rendu Remotion headless — image de mesure (décision bloquante #1).
FROM node:22-bookworm-slim

# Dépendances Chrome Headless Shell + ffmpeg + GNU time (pic RSS) + polices.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg time \
    fonts-liberation fonts-noto-core fonts-noto-color-emoji \
    libnss3 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 \
    libpango-1.0-0 libcairo2 libasound2 libatspi2.0-0 libxshmfence1 \
    libx11-6 libxcb1 libxext6 libxi6 libglib2.0-0 \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps (cache séparé du code).
COPY package.json ./
RUN npm install --no-audit --no-fund

# Pré-télécharge le Chrome Headless Shell (couche indépendante du code -> pas de re-download).
RUN npx remotion browser ensure

COPY remotion.config.ts tsconfig.json ./
COPY src ./src
COPY scripts ./scripts

# Paramètres par défaut (override via -e au run).
ENV DURATION_S=60
ENV RATE_USD_PER_HR=0.197

CMD ["node", "scripts/measure.mjs"]
