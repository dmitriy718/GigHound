import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .auth import validate_auth_config
from .config import BEHIND_TLS, CORS_ORIGINS
from .routers import (adapters, alerts, analytics, auth, credentials, filters,
                      gigs, jobs, keywords, orchestration, profiles, proposals)
from .ws_manager import alerts as ws_manager

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail fast without a JWT secret (unless GIGHOUND_DEV_NOAUTH=1).
    # Schema is managed by Alembic: run `alembic upgrade head` before starting.
    validate_auth_config()
    await ws_manager.start_subscriber()  # AD-6: Redis pub/sub fan-in
    yield
    await ws_manager.stop_subscriber()


app = FastAPI(title="GigHound", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


from .request_bounds import RequestBounds
app.add_middleware(RequestBounds)


@app.middleware("http")
async def security_headers(request, call_next):
    """Baseline response hardening. The CSP allows the built SPA to function:
    scripts/styles/images from self, inline styles (the React build emits
    style attributes), no inline scripts. HSTS only when the deployment is
    behind TLS (GIGHOUND_BEHIND_TLS=1) — serving it over plain HTTP would be
    meaningless and break local development."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'")
    if BEHIND_TLS:
        response.headers["Strict-Transport-Security"] = \
            "max-age=31536000; includeSubDomains"
    return response

app.include_router(auth.router)
app.include_router(keywords.router)
app.include_router(filters.router)
app.include_router(jobs.router)
app.include_router(alerts.router)
app.include_router(profiles.router)
app.include_router(adapters.router)
app.include_router(proposals.router)
app.include_router(orchestration.router)
app.include_router(credentials.router)
app.include_router(gigs.router)
app.include_router(analytics.router)
from . import workbench
app.include_router(workbench.router)
from . import teamwork
app.include_router(teamwork.router)


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/ready")
def readiness():
    from fastapi.responses import JSONResponse
    from sqlalchemy import text
    from .database import SessionLocal
    from .cache import cache
    checks = {"database": False, "broker": False}
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
            db.execute(text("SELECT job_id FROM generation_work LIMIT 1"))
            db.execute(text("SELECT revision FROM automation_circuits LIMIT 1"))
            checks["database"] = True
    except Exception:
        pass
    try:
        client = cache._client()
        checks["broker"] = bool(client and client.ping())
    except Exception:
        pass
    ready = all(checks.values())
    return JSONResponse({"status": "ready" if ready else "unavailable", "checks": checks}, status_code=200 if ready else 503)


# --- Frontend SPA (Phase 4.1) ---
# The backend serves the built frontend when a dist directory exists: in
# Docker the image builds it (backend/Dockerfile stage 1) and points
# GIGHOUND_FRONTEND_DIST at it; locally `npm run build` in frontend/
# produces the same path. API and WS routes are registered above and take
# precedence; unknown non-API paths fall back to index.html so client-side
# routing works on refresh.
FRONTEND_DIST = Path(
    os.getenv("GIGHOUND_FRONTEND_DIST")
    or Path(__file__).resolve().parents[2] / "frontend" / "dist"
)


class SPAStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and not path.startswith(("api/", "ws/")):
                return await super().get_response("index.html", scope)
            raise


if FRONTEND_DIST.is_dir():
    app.mount("/", SPAStaticFiles(directory=str(FRONTEND_DIST), html=True),
              name="frontend")
