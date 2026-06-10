FROM node:20-slim
WORKDIR /app
# Baileys depende de libsignal vía una URL de git → git es obligatorio para npm install.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY package.json ./
# --legacy-peer-deps evita los conflictos ERESOLVE de los peer deps de Baileys.
RUN npm install --omit=dev --no-audit --no-fund --legacy-peer-deps
COPY src ./src
ENV PORT=8080
EXPOSE 8080
CMD ["node", "src/index.js"]
