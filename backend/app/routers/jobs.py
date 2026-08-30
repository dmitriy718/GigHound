import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..auth import get_current_user, get_owned, scoped
from ..cache import cache
from ..database import get_db
from ..ingest import run_ingest, scoring_keywords  # noqa: F401 — re-exported
from ..models import Job, User
from ..schemas import (BulkArchiveAction, IngestJobsIn, IngestResult, JobOut,
                       ScorePreviewIn, ScorePreviewOut)

router = APIRouter(prefix="/api/jobs", tags=["jobs"])
log = logging.getLogger(__name__)

INGEST_RATE_LIMIT = 30          # requests per window per user
INGEST_RATE_WINDOW_SECONDS = 60


def _check_ingest_rate(user: User) -> None:
    """429 when the ingest bucket overflows; graceful no-op if Redis is down."""
    if cache._r is None:
        return
    key = f"ingest_rate:{user.id}"
    try:
        hits = cache._r.incr(key)
        if hits == 1:
            cache._r.expire(key, INGEST_RATE_WINDOW_SECONDS)
    except Exception:  # noqa: BLE001 — the limiter must never block ingestion
        log.warning("ingest rate limiter unavailable; skipping")
        return
    if hits > INGEST_RATE_LIMIT:
        raise HTTPException(429, f"ingest rate limit exceeded ({INGEST_RATE_LIMIT}/min)")


@router.get("", response_model=dict)
def list_jobs(
    status: str | None = Query(None),
    platform: str | None = Query(None),
    min_score: float | None = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = scoped(db, Job, user)
    if status:
        q = q.filter(Job.status == status)
    if platform:
        q = q.filter(Job.platform == platform)
    if min_score is not None:
        q = q.filter(Job.quality_score >= min_score)
    total = q.count()
    jobs = q.order_by(Job.fetched_at.desc()).offset(offset).limit(limit).all()
    return {"jobs": [JobOut.model_validate(j) for j in jobs], "total": total}


@router.get("/{job_id}", response_model=JobOut)
def get_job(job_id: int, db: Session = Depends(get_db),
            user: User = Depends(get_current_user)):
    job = get_owned(db, Job, job_id, user)
    if not job:
        raise HTTPException(404, "job not found")
    from ..client_intel import client_history_for_job
    out = JobOut.model_validate(job)
    out.client_history = client_history_for_job(db, user.id, job)
    return out


@router.post("/{job_id}/archive", response_model=JobOut)
def archive_job(job_id: int, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    job = get_owned(db, Job, job_id, user)
    if not job:
        raise HTTPException(404, "job not found")
    job.status = "archived"
    db.commit()
    db.refresh(job)
    cache.invalidate_prefix("preview:")
    return job


@router.post("/bulk-archive", response_model=dict)
def bulk_archive_jobs(body: BulkArchiveAction, db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    """Archive many jobs at once. Unknown/foreign ids land in `skipped`."""
    archived, skipped = [], []
    for jid in body.ids:
        job = get_owned(db, Job, jid, user)
        if not job or job.status == "archived":
            skipped.append(jid)
            continue
        job.status = "archived"
        archived.append(jid)
    db.commit()
    cache.invalidate_prefix("preview:")
    return {"archived": archived, "skipped": skipped}


@router.post("/{job_id}/unarchive", response_model=JobOut)
def unarchive_job(job_id: int, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    job = get_owned(db, Job, job_id, user)
    if not job:
        raise HTTPException(404, "job not found")
    job.status = "new"
    db.commit()
    db.refresh(job)
    cache.invalidate_prefix("preview:")
    return job


@router.post("/ingest", response_model=IngestResult)
async def ingest_jobs(body: IngestJobsIn, db: Session = Depends(get_db),
                      user: User = Depends(get_current_user)):
    """Adapter entry point: score, dedupe, auto-archive, alert.

    Typed body (422 on malformed input) + per-user Redis token bucket —
    this is the untrusted-input entry point and an LLM-cost amplifier.
    """
    _check_ingest_rate(user)
    return await run_ingest(body, db, user)


@router.post("/score-preview", response_model=ScorePreviewOut)
def score_preview(body: ScorePreviewIn, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """Dry-run scoring (ScoringConfig playground): pure scoring, persists
    nothing, queues nothing. Negative-keyword semantics identical to ingest."""
    from ..ingest import _market_rate_for
    from ..scoring import compute_quality_score

    return compute_quality_score(body.job, scoring_keywords(db, user),
                                 market_rate=_market_rate_for(db, user.id, body.job))
