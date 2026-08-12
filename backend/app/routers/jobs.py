import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from rapidfuzz import fuzz
from sqlalchemy.orm import Session

from ..cache import cache
from ..database import get_db
from ..models import AlertSettings, Job, KeywordGroup, SearchFilter
from ..schemas import IngestResult, JobIngest, JobOut
from ..scoring import compute_quality_score, to_usd
from ..ws_manager import alerts

router = APIRouter(prefix="/api/jobs", tags=["jobs"])
log = logging.getLogger(__name__)


@router.get("", response_model=dict)
def list_jobs(
    status: str | None = Query(None),
    platform: str | None = Query(None),
    min_score: float | None = Query(None),
    limit: int = Query(50, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    q = db.query(Job)
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
def get_job(job_id: int, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


@router.post("/{job_id}/archive", response_model=JobOut)
def archive_job(job_id: int, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    job.status = "archived"
    db.commit()
    db.refresh(job)
    cache.invalidate_prefix("preview:")
    return job


@router.post("/{job_id}/unarchive", response_model=JobOut)
def unarchive_job(job_id: int, db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    job.status = "new"
    db.commit()
    db.refresh(job)
    cache.invalidate_prefix("preview:")
    return job


def _market_rate_for(db: Session, item) -> float | None:
    """Hourly market rate for budget realism: rate card match → None (default)."""
    from ..orchestrator import pick_rate
    from ..models import Job as _Job  # pick_rate expects a Job-like with skills/title

    probe = _Job(title=item.title, external_id=item.external_id,
                 platform=item.platform, skills=item.skills)
    entry = pick_rate(db, probe)
    return entry.hourly_rate if entry and entry.hourly_rate else None


def _find_duplicate(db: Session, job: Job) -> Job | None:
    """Cross-platform duplicate detection via fuzzy title+description match."""
    candidates = (
        db.query(Job)
        .filter(Job.status != "archived", Job.id != job.id)
        .order_by(Job.fetched_at.desc())
        .limit(200)
        .all()
    )
    for other in candidates:
        title_sim = fuzz.token_set_ratio(job.title.lower(), other.title.lower())
        if title_sim >= 90:
            desc_sim = fuzz.token_set_ratio(
                (job.description or "")[:500].lower(), (other.description or "")[:500].lower()
            )
            if desc_sim >= 80:
                return other
    return None


@router.post("/ingest", response_model=IngestResult)
async def ingest_jobs(body: dict, db: Session = Depends(get_db)):
    """Adapter entry point: score, dedupe, auto-archive, alert."""
    items = [JobIngest(**j) for j in body.get("jobs", [])]
    settings = db.query(AlertSettings).first()
    filters = db.query(SearchFilter).all()
    groups = {g.id: g for g in db.query(KeywordGroup).all()}

    ingested = auto_archived = alerts_sent = 0
    for item in items:
        existing = (
            db.query(Job)
            .filter(Job.platform == item.platform, Job.external_id == item.external_id)
            .first()
        )
        if existing:
            continue

        job = Job(**item.model_dump())
        job.budget_usd_min = to_usd(item.budget_min, item.currency)
        job.budget_usd_max = to_usd(item.budget_max, item.currency)

        # Score against the union of all keyword groups referenced by filters;
        # fall back to all groups if no filter references one.
        referenced = {f.keyword_group_id for f in filters if f.keyword_group_id}
        kws = []
        for gid in (referenced or set(groups.keys())):
            g = groups.get(gid)
            if g:
                kws.extend(g.keywords)
        scored = compute_quality_score(item, kws, market_rate=_market_rate_for(db, item))
        job.quality_score = scored["quality_score"]
        job.score_breakdown = scored["score_breakdown"]
        job.red_flags = scored["red_flags"]

        db.add(job)
        db.flush()  # assign id before dedup check

        dup = _find_duplicate(db, job)
        if dup:
            job.is_duplicate = True
            job.duplicate_of = dup.id
            job.red_flags = (job.red_flags or []) + ["duplicate posting"]

        # Auto-archive below the strictest (lowest) active threshold
        thresholds = [f.quality_threshold for f in filters]
        if thresholds and job.quality_score < min(thresholds):
            job.status = "archived"
            auto_archived += 1
            db.commit()
            continue

        db.commit()
        db.refresh(job)
        ingested += 1

        # Real-time feed: every ingested job streams to the dashboard
        job_payload = JobOut.model_validate(job).model_dump(mode="json")
        await alerts.broadcast({"type": "job_ingested", "job": job_payload})

        # Auto-match pipeline: score cleared threshold → draft proposal → review queue
        from ..orchestrator import maybe_queue_proposal
        queued = await maybe_queue_proposal(db, job)
        if queued:
            db.refresh(job)

        # Real-time / hot-job alerts (hot: ≥90 score, <5 proposals, posted <1h by default)
        if settings and settings.realtime_enabled and job.quality_score >= settings.min_score_alert:
            now = datetime.now(timezone.utc)
            posted = job.posted_at
            if posted and posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)
            fresh = posted and (now - posted).total_seconds() / 3600 <= settings.hot_job_posted_hours
            quiet = (job.proposals_count or 0) < settings.hot_job_max_proposals
            strong = job.quality_score >= getattr(settings, "hot_job_min_score", 90.0)
            msg_type = "hot_job" if (settings.hot_job_enabled and fresh and quiet and strong) else "job_alert"
            await alerts.broadcast({"type": msg_type, "job": job_payload})
            job.status = "notified"
            db.commit()
            alerts_sent += 1

    cache.invalidate_prefix("preview:")
    return IngestResult(ingested=ingested, auto_archived=auto_archived, alerts_sent=alerts_sent)
