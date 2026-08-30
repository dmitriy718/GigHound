# syntax=docker/dockerfile:1
# OPTIONAL standalone frontend image (static bundle served by nginx).
# The default deployment path needs no separate web container: the backend
# image (backend/Dockerfile) already builds this dist and serves it via
# FastAPI StaticFiles. Use this image only if you want to serve the SPA
# from a dedicated web tier. Build context: frontend/.
# Note: VITE_API_URL is baked in at build time
# (docker build --build-arg VITE_API_URL=https://api.example.com frontend/).
FROM node:20-slim AS build
WORKDIR /web
ARG VITE_API_URL=http://localhost:8000
ENV VITE_API_URL=${VITE_API_URL}
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /web/dist /usr/share/nginx/html
EXPOSE 80
