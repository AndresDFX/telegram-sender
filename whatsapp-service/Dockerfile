FROM node:20-slim
WORKDIR /app
COPY package.json ./
# --legacy-peer-deps evita los conflictos ERESOLVE de los peer deps de Baileys.
RUN npm install --omit=dev --no-audit --no-fund --legacy-peer-deps
COPY src ./src
ENV PORT=8080
EXPOSE 8080
CMD ["node", "src/index.js"]
