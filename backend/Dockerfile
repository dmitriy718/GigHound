# syntax=docker/dockerfile:1
# GigHound all-in-one image: frontend build + backend runtime.
# Build context is the REPO ROOT (frontend source is needed in stage 1):
#   docker build -f backend/Dockerfile .

# ---- Stage 1: build the frontend bundle ----
FROM node:20-slim AS frontend
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- Stage 2: backend runtime (serves API + the dist bundle) ----
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GIGHOUND_IN_DOCKER=1 \
    GIGHOUND_FRONTEND_DIST=/app/frontend/dist

WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/alembic.ini backend/alembic.ini
COPY backend/alembic/ backend/alembic/
COPY backend/app/ backend/app/
COPY backend/scripts/ backend/scripts/
COPY --from=frontend /web/dist frontend/dist

RUN useradd --create-home --uid 10001 gighound && chown -R gighound:gighound /app
USER gighound

WORKDIR /app/backend
EXPOSE 8000
# Schema is managed by Alembic (AD-3): migrate, then serve. `exec` makes
# uvicorn PID 1's replacement so SIGTERM from `docker stop` is forwarded.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health', timeout=5)"
