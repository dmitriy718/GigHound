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
# npm ci uses the committed package lock; image tags must be requalified on updates.
FROM node:26-slim@sha256:c0753125a3789977aefe869cbebccf70e3cfd7ea84ca48547458f02e4f1d7146 AS build
WORKDIR /web
ARG VITE_API_URL=""
ENV VITE_API_URL=${VITE_API_URL}
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:1.31.4-alpine@sha256:db35bfc6b2951e7f8a72db5db120288c127ffaeeb4a6d4b95a26fead017d5913
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html
EXPOSE 80
