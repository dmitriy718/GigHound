"""The ingest pipeline, shared by the HTTP endpoint, adapter search
endpoints, and the scheduled discovery beat.

Per call: dedupe → score → auto-archive → persist → WS job_ingested →
enqueue proposal generation (Celery task; the LLM call never runs on the
request path). Rate card / portfolio / search profiles (+ parsed boolean
ASTs) / linked filters are preloaded ONCE per call (PipelineContext), not
per job.
"""
import logging
from datetime import datetime, timedelta, timezone

import redis
from rapidfuzz import fuzz
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import scoped
from .cache import cache
from .models import AlertSettings, Job, KeywordGroup, SearchFilter, User
from .orchestrator import (build_pipeline_context, enqueue_generation_if_eligible)
from .schemas import IngestJobsIn, IngestResult, JobOut
from .scoring import compute_quality_score, to_usd
from .ws_manager import alerts

log = logging.getLogger(__name__)

DEDUPE_WINDOW = timedelta(hours=72)
DEDUPE_CANDIDATE_CAP = 500


def _market_rate_for(db: Session, user_id: int, item, rate_entries=None) -> float | None:
    """Hourly market rate for budget realism: rate card match → None (default)."""
    from .orchestrator import pick_rate
    from .models import Job as _Job  # pick_rate expects a Job-like with skills/title

    probe = _Job(title=item.title, external_id=item.external_id,
                 platform=item.platform, skills=item.skills)
    entry = pick_rate(db, user_id, probe, entries=rate_entries)
    return entry.hourly_rate if entry and entry.hourly_rate else None


def _find_duplicate(db: Session, user_id: int, job: Job) -> Job | None:
    """Duplicate detection via fuzzy title+description match.

    Candidates are bounded: same tenant, same platform, fetched within the
    last 72h, capped — kills the old last-200-rows scan (which both missed
    anything older than 200 rows and went quadratic on bulk ingest).
    """
    cutoff = datetime.now(timezone.utc) - DEDUPE_WINDOW
    candidates = (
        db.query(Job)
        .filter(Job.user_id == user_id, Job.platform == job.platform,
                Job.status != "archived", Job.id != job.id,
                Job.fetched_at >= cutoff)
        .order_by(Job.fetched_at.desc())
        .limit(DEDUPE_CANDIDATE_CAP)
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


def scoring_keywords(db: Session, user: User) -> list:
    """Union of keyword groups referenced by filters (all groups when none
    are referenced) — the same semantics ingest scores with."""
    filters = scoped(db, SearchFilter, user).all()
    groups = {g.id: g for g in scoped(db, KeywordGroup, user).all()}
    referenced = {f.keyword_group_id for f in filters if f.keyword_group_id}
    kws = []
    for gid in (referenced or set(groups.keys())):
        g = groups.get(gid)
        if g:
            kws.extend(g.keywords)
    return kws


async def run_ingest(body: IngestJobsIn, db: Session, user: User) -> IngestResult:
    """Score, dedupe, auto-archive, alert, and enqueue proposal generation."""
    items = body.jobs
    settings = db.query(AlertSettings).filter(AlertSettings.user_id == user.id).first()
    filters = scoped(db, SearchFilter, user).all()
    groups = {g.id: g for g in scoped(db, KeywordGroup, user).all()}
    ctx = build_pipeline_context(db, user.id)

    ingested = auto_archived = alerts_sent = 0
    for item in items:
        existing = (
            db.query(Job)
            .filter(Job.user_id == user.id, Job.platform == item.platform,
                    Job.external_id == item.external_id)
            .first()
        )
        if existing:
            continue

        job = Job(user_id=user.id, **item.model_dump())
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
        scored = compute_quality_score(item, kws, market_rate=_market_rate_for(
            db, user.id, item, rate_entries=ctx.rate_entries))
        job.quality_score = scored["quality_score"]
        job.score_breakdown = scored["score_breakdown"]
        job.red_flags = scored["red_flags"]

        db.add(job)
        try:
            db.flush()  # assign id before dedup check
        except IntegrityError:
            # unique (user_id, platform, external_id) — concurrent ingest race
            db.rollback()
            continue

        dup = _find_duplicate(db, user.id, job)
        if dup:
            job.is_duplicate = True
            job.duplicate_of = dup.id
            job.red_flags = (job.red_flags or []) + ["duplicate posting"]

        # Negative-keyword exclusion archives unconditionally, thresholds or not
        if (job.score_breakdown or {}).get("excluded_by_negative_keyword"):
            job.status = "archived"
            auto_archived += 1
            db.commit()
            continue

        # Auto-archive only below the LOWEST active threshold: a job at or
        # above it may still pass the most lenient filter (per-filter gating
        # lives in filtering.py), so min() is the correct cutoff here
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
        await alerts.broadcast(user.id, {"type": "job_ingested", "job": job_payload})

        # Auto-match pipeline: gates run inline; the LLM draft is enqueued as
        # a Celery task (proposal generation never blocks the request path).
        enqueue_generation_if_eligible(db, job, ctx)

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
            await alerts.broadcast(user.id, {"type": msg_type, "job": job_payload})
            job.status = "notified"
            db.commit()
            alerts_sent += 1

    # jobs are already committed — a Redis failure here must not 500 the
    # ingest; a stale preview cache is the lesser evil
    try:
        cache.invalidate_prefix("preview:")
    except redis.RedisError as exc:
        log.warning("preview cache invalidation failed (%s); continuing", exc)
    return IngestResult(ingested=ingested, auto_archived=auto_archived, alerts_sent=alerts_sent)
