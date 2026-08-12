import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import CORS_ORIGINS
from .database import Base, engine
from .routers import (adapters, alerts, filters, gigs, jobs, keywords,
                      orchestration, profiles, proposals)

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="GigHound", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(keywords.router)
app.include_router(filters.router)
app.include_router(jobs.router)
app.include_router(alerts.router)
app.include_router(profiles.router)
app.include_router(adapters.router)
app.include_router(proposals.router)
app.include_router(orchestration.router)
app.include_router(gigs.router)


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)


@app.get("/api/health")
def health():
    return {"status": "ok"}
