# syntax=docker/dockerfile:1
# OPTIONAL standalone frontend image (static bundle served by nginx).
# The default deployment path needs no separate web container: the backend
# image (backend/Dockerfile) already builds this dist and serves it via
# FastAPI StaticFiles. Use this image only if you want to serve the SPA
# from a dedicated web tier. Build context: frontend/.
# Note: VITE_API_URL is baked in at build time and defaults to "" (same
# origin) because nginx.conf proxies /api/ and /ws/ to the backend service.
# Override only when the API lives on a different origin:
#   docker build --build-arg VITE_API_URL=https://api.example.com frontend/
# node stays at major-only pinning: the 26.x line is LTS-stable (no breaking
# minor releases), and npm ci is already locked by package-lock.json.
FROM node:26-slim AS build
WORKDIR /web
ARG VITE_API_URL=""
ENV VITE_API_URL=${VITE_API_URL}
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:1.31.4-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html
EXPOSE 80
