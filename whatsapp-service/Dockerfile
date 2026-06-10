FROM node:22-slim
WORKDIR /app
# Baileys depende de libsignal vía una URL de git (ssh://git@github.com/...). Necesitamos
# git + ca-certificates y reescribir esas URLs SSH a HTTPS para poder clonar el repo
# público de forma anónima (sin clave SSH).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && git config --global url."https://github.com/".insteadOf "ssh://git@github.com/" \
    && git config --global --add url."https://github.com/".insteadOf "git@github.com:"
COPY package.json ./
# --legacy-peer-deps evita los conflictos ERESOLVE de los peer deps de Baileys.
RUN npm install --omit=dev --no-audit --no-fund --legacy-peer-deps
COPY src ./src
ENV PORT=8080
EXPOSE 8080
CMD ["node", "src/index.js"]
