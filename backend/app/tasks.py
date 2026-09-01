"""Celery app + beat schedule.

Tasks are thin wrappers around directly-callable cores (test env has no
broker/worker): import the core (or call the task object like a function)
to run synchronously. Stealth-site interaction itself is executed by the
external stealth-browser worker pool, which polls
`GET /api/gigs/stealth-tasks` and posts results back.

Run:
    celery -A app.tasks worker --loglevel=info
    celery -A app.tasks beat --loglevel=info
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from celery import Celery
from celery.schedules import crontab

from .config import REDIS_URL
from .database import SessionLocal
from .fiverr_monitor import enqueue_buyer_request_fetch
from .gig_analytics import enqueue_metrics_scrape
from .models import Job, ProposalQueueItem, User

log = logging.getLogger(__name__)

celery_app = Celery("gighound", broker=REDIS_URL, backend=REDIS_URL)

# JSON-only serialization: the broker is not a trusted channel (no pickle RCE).
celery_app.conf.update(
    accept_content=["json"],
    task_serializer="json",
    result_serializer="json",
)

celery_app.conf.beat_schedule = {
    "fiverr-buyer-request-monitor": {
        "task": "app.tasks.fiverr_buyer_request_tick",
        "schedule": 15 * 60,  # every 15 minutes
    },
    "gig-analytics-weekly": {
        "task": "app.tasks.gig_analytics_tick",
        "schedule": crontab(hour=6, minute=12, day_of_week="mon"),
    },
    "discovery": {
        # every 15 minutes, offset from the fiverr tick
        "task": "app.tasks.discovery_tick",
        "schedule": crontab(minute="8,23,38,53"),
    },
    "outcome-sync": {
        "task": "app.tasks.outcome_sync_tick",
        "schedule": 30 * 60,  # every 30 minutes
    },
    "upwork-outcome-sync": {
        # Upwork has no compliant status API for agency submissions — the
        # browser worker scrapes the proposals page read-only instead.
        "task": "app.tasks.upwork_outcome_tick",
        "schedule": 60 * 60,  # every 60 minutes
    },
    "generation-retry": {
        "task": "app.tasks.generation_retry_tick",
        "schedule": 30 * 60,  # every 30 minutes
    },
    "digest": {
        "task": "app.tasks.digest_tick",
        "schedule": crontab(minute=41),  # hourly
    },
    "auto-archive": {
        "task": "app.tasks.auto_archive_tick",
        "schedule": crontab(hour=3, minute=17),  # daily
    },
    "follow-up-due": {
        "task": "app.tasks.follow_up_due_tick",
        "schedule": crontab(hour=9, minute=5),  # daily 09:05 UTC
    },
    "retention": {
        "task": "app.tasks.retention_tick",
        "schedule": crontab(hour=4, minute=11),  # daily 04:11 UTC
    },
    "stealth-reaper": {
        # reset tasks stuck in `claimed` when a worker dies mid-task
        "task": "app.tasks.stealth_reaper_tick",
        "schedule": crontab(minute="4,9,14,19,24,29,34,39,44,49,54,59"),
    },
}
celery_app.conf.timezone = "UTC"

GENERATION_RETRY_WINDOW = timedelta(hours=24)
GENERATION_MAX_RETRIES = 2
AUTO_ARCHIVE_STALE = timedelta(days=14)
RETENTION_ARCHIVED_JOB_AGE = timedelta(days=90)
RETENTION_STEALTH_TASK_AGE = timedelta(days=30)
RETENTION_AUDIT_AGE = timedelta(days=365)
STEALTH_CLAIM_TIMEOUT = timedelta(minutes=15)
STEALTH_MAX_RECLAIMS = 3


@celery_app.task(name="app.tasks.fiverr_buyer_request_tick")
def fiverr_buyer_request_tick() -> dict:
    """Enqueue a buyer-request fetch for the stealth worker (circuit-gated).

    One fetch task per active user — stealth tasks are tenant-owned (AD-1).
    """
    return fiverr_buyer_request_tick_core()


def fiverr_buyer_request_tick_core() -> dict:
    from .models import PlatformAccount, StealthTask

    db = SessionLocal()
    try:
        enqueued = []
        for user in db.query(User).filter(User.is_active.is_(True)).all():
            try:
                # account-less users can't be scraped — don't generate doomed
                # tasks that fail in the worker and feed the circuit breaker
                has_fiverr = (db.query(PlatformAccount)
                              .filter(PlatformAccount.user_id == user.id,
                                      PlatformAccount.platform == "fiverr",
                                      PlatformAccount.enabled.is_(True),
                                      PlatformAccount.mode != "disabled")
                              .first())
                if has_fiverr is None:
                    continue
                # don't stack fetches while a previous one is still in flight
                # (e.g. all workers down — dedupe like proposal_status_sync)
                in_flight = (db.query(StealthTask)
                             .filter(StealthTask.user_id == user.id,
                                     StealthTask.platform == "fiverr",
                                     StealthTask.task_type == "fiverr_fetch_buyer_requests",
                                     StealthTask.status.in_(("pending", "claimed")))
                             .first())
                if in_flight is not None:
                    continue
                task = enqueue_buyer_request_fetch(db, user.id)
                enqueued.append(task.id if task else None)
            except Exception as exc:  # noqa: BLE001 — per-user isolation
                log.exception("buyer request fetch enqueue failed for user %d (%s)",
                              user.id, exc)
                db.rollback()
        return {"enqueued": enqueued}
    finally:
        db.close()


@celery_app.task(name="app.tasks.gig_analytics_tick")
def gig_analytics_tick() -> dict:
    """Weekly: enqueue per-platform metrics scrapes (circuit-gated), per user."""
    return gig_analytics_tick_core()


def gig_analytics_tick_core() -> dict:
    db = SessionLocal()
    try:
        enqueued = []
        for user in db.query(User).filter(User.is_active.is_(True)).all():
            try:
                enqueued.extend(t.id for t in enqueue_metrics_scrape(db, user.id))
            except Exception as exc:  # noqa: BLE001 — per-user isolation
                log.exception("gig analytics enqueue failed for user %d (%s)",
                              user.id, exc)
                db.rollback()
        return {"enqueued": enqueued}
    finally:
        db.close()


# ---------------- Phase 2: learning loop ----------------

@celery_app.task(name="app.tasks.generate_proposal_task")
def generate_proposal_task(job_id: int) -> dict:
    """Thin wrapper — the core below is directly callable in tests."""
    return generate_proposal_core(job_id)


def generate_proposal_core(job_id: int) -> dict:
    """Generate + queue a proposal for one job (off the request path).

    Re-runs the full gate set (idempotent), and retries generation_failed
    items in place when one exists for the job.
    """
    from .orchestrator import maybe_queue_proposal, regenerate_failed_item

    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            return {"queued": False, "reason": "job not found"}
        failed = (db.query(ProposalQueueItem)
                  .filter(ProposalQueueItem.job_id == job.id,
                          ProposalQueueItem.status == "generation_failed")
                  .order_by(ProposalQueueItem.created_at.desc())
                  .first())
        if failed is not None:
            item = asyncio.run(regenerate_failed_item(db, failed))
        else:
            item = asyncio.run(maybe_queue_proposal(db, job))
        return {
            "queued": item is not None and item.status == "pending_review",
            "proposal_id": item.id if item else None,
        }
    finally:
        db.close()


@celery_app.task(name="app.tasks.discovery_tick")
def discovery_tick() -> dict:
    """Beat entrypoint — fan-out dispatcher (see discovery_tick_core)."""
    return discovery_tick_core()


def discovery_tick_core() -> dict:
    """Dispatcher: enqueue one discover_profile_task per (active user,
    search profile) and return immediately. The heavy per-profile discovery
    runs in `discover_profile_task` workers, not in the beat tick — one slow
    profile can no longer starve the others (scalability: fan-out beats)."""
    from .models import SearchProfile

    db = SessionLocal()
    try:
        enqueued = []
        pairs = (db.query(SearchProfile.user_id, SearchProfile.id)
                 .join(User, SearchProfile.user_id == User.id)
                 .filter(User.is_active.is_(True))
                 .all())
        for user_id, profile_id in pairs:
            try:
                discover_profile_task.delay(user_id, profile_id)
                enqueued.append(f"{user_id}:{profile_id}")
            except Exception as exc:  # noqa: BLE001 — broker down; next tick retries
                log.warning("discovery dispatch failed for profile %d (%s)",
                            profile_id, exc)
        return {"enqueued": enqueued}
    finally:
        db.close()


@celery_app.task(name="app.tasks.discover_profile_task")
def discover_profile_task(user_id: int, profile_id: int) -> dict:
    """Thin wrapper — the core below is directly callable in tests."""
    return discover_profile_core(user_id, profile_id)


def discover_profile_core(user_id: int, profile_id: int) -> dict:
    """Run discovery for a single (user, profile) pair."""
    from .discovery import run_profile_discovery
    from .models import SearchProfile

    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        profile = db.get(SearchProfile, profile_id)
        if user is None or profile is None or profile.user_id != user_id:
            return {"queued": False, "reason": "user/profile not found"}
        try:
            return asyncio.run(run_profile_discovery(db, user, profile))
        except Exception as exc:  # noqa: BLE001 — isolate per-profile failures
            log.exception("discovery failed for profile %d (%s)", profile_id, exc)
            db.rollback()
            return {"queued": False, "reason": str(exc)}
    finally:
        db.close()


@celery_app.task(name="app.tasks.outcome_sync_tick")
def outcome_sync_tick() -> dict:
    """Beat entrypoint — fan-out dispatcher (see outcome_sync_tick_core)."""
    return outcome_sync_tick_core()


def outcome_sync_tick_core() -> dict:
    """Dispatcher: enqueue one outcome_sync_user_task per active user and
    return immediately (fan-out beats — one user's adapter latency/failure
    no longer delays the others)."""
    db = SessionLocal()
    try:
        enqueued = []
        for (user_id,) in db.query(User.id).filter(User.is_active.is_(True)).all():
            try:
                outcome_sync_user_task.delay(user_id)
                enqueued.append(user_id)
            except Exception as exc:  # noqa: BLE001 — broker down; next tick retries
                log.warning("outcome sync dispatch failed for user %d (%s)",
                            user_id, exc)
        return {"enqueued": enqueued}
    finally:
        db.close()


@celery_app.task(name="app.tasks.outcome_sync_user_task")
def outcome_sync_user_task(user_id: int) -> dict:
    """Thin wrapper — the core below is directly callable in tests."""
    return outcome_sync_user_core(user_id)


def outcome_sync_user_core(user_id: int) -> dict:
    """Poll bid statuses + message threads for one tenant's open proposals."""
    from .outcome_sync import sync_user_outcomes

    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if user is None:
            return {"checked": 0, "outcomes": 0, "replies": 0}
        try:
            return asyncio.run(sync_user_outcomes(db, user))
        except Exception as exc:  # noqa: BLE001 — per-user isolation
            log.exception("outcome sync failed for user %d (%s)", user_id, exc)
            db.rollback()
            return {"checked": 0, "outcomes": 0, "replies": 0, "error": str(exc)}
    finally:
        db.close()


@celery_app.task(name="app.tasks.upwork_outcome_tick")
def upwork_outcome_tick() -> dict:
    """Beat entrypoint — fan-out dispatcher (see upwork_outcome_tick_core)."""
    return upwork_outcome_tick_core()


def upwork_outcome_tick_core() -> dict:
    """Dispatcher: enqueue one upwork_outcome_user_task per active user
    (fan-out beats — matches the other sync ticks). The task name is kept
    stable for the beat schedule, but the sync now covers every browser
    platform (upwork/fiverr/peopleperhour/guru), not just upwork."""
    db = SessionLocal()
    try:
        enqueued = []
        for (user_id,) in db.query(User.id).filter(User.is_active.is_(True)).all():
            try:
                upwork_outcome_user_task.delay(user_id)
                enqueued.append(user_id)
            except Exception as exc:  # noqa: BLE001 — broker down; next tick retries
                log.warning("platform outcome dispatch failed for user %d (%s)",
                            user_id, exc)
        return {"enqueued": enqueued}
    finally:
        db.close()


@celery_app.task(name="app.tasks.upwork_outcome_user_task")
def upwork_outcome_user_task(user_id: int) -> dict:
    """Thin wrapper — the core below is directly callable in tests."""
    return upwork_outcome_user_core(user_id)


def upwork_outcome_user_core(user_id: int) -> dict:
    """Enqueue read-only scrape_proposal_status stealth tasks for one tenant —
    one per browser platform (upwork/fiverr/peopleperhour/guru) that has an
    enabled account and open proposals. (Task name kept stable for beat.)"""
    from .proposal_status_sync import enqueue_platform_status_scrapes

    db = SessionLocal()
    try:
        try:
            tasks = enqueue_platform_status_scrapes(db, user_id)
        except Exception as exc:  # noqa: BLE001 — per-user isolation
            log.exception("platform status scrape enqueue failed for user %d (%s)",
                          user_id, exc)
            db.rollback()
            return {"enqueued": 0, "error": str(exc)}
        return {"enqueued": len(tasks),
                "task_ids": [t.id for t in tasks]}
    finally:
        db.close()


@celery_app.task(name="app.tasks.generation_retry_tick")
def generation_retry_tick() -> dict:
    """Re-enqueue generation for recent generation_failed items (max 2 retries;
    the retry count rides in submission_result)."""
    return generation_retry_tick_core()


def generation_retry_tick_core() -> dict:
    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - GENERATION_RETRY_WINDOW
        items = (db.query(ProposalQueueItem)
                 .filter(ProposalQueueItem.status == "generation_failed",
                         ProposalQueueItem.created_at >= cutoff)
                 .all())
        retried = []
        for item in items:
            result = dict(item.submission_result or {})
            retries = int(result.get("generation_retries") or 0)
            if retries >= GENERATION_MAX_RETRIES:
                continue
            result["generation_retries"] = retries + 1
            item.submission_result = result
            db.commit()
            try:
                generate_proposal_task.delay(item.job_id)
                retried.append(item.id)
            except Exception as exc:  # noqa: BLE001 — broker down; try next tick
                log.warning("generation retry enqueue failed for item %d (%s)",
                            item.id, exc)
        return {"retried": retried}
    finally:
        db.close()


@celery_app.task(name="app.tasks.digest_tick")
def digest_tick() -> dict:
    """Hourly: fan out one digest_user_task per due user (see digest_tick_core)."""
    return digest_tick_core()


def digest_tick_core() -> dict:
    """Dispatcher: enqueue one digest_user_task per user whose digest mode is
    due (hourly always; daily at 07:00 UTC) and return immediately — one
    user's blocking SMTP send no longer delays the others (fan-out beats)."""
    from .digest import due_digest_user_ids

    db = SessionLocal()
    try:
        enqueued = []
        for user_id in due_digest_user_ids(db):
            try:
                digest_user_task.delay(user_id)
                enqueued.append(user_id)
            except Exception as exc:  # noqa: BLE001 — broker down; next tick retries
                log.warning("digest dispatch failed for user %d (%s)",
                            user_id, exc)
        return {"enqueued": enqueued}
    finally:
        db.close()


@celery_app.task(name="app.tasks.digest_user_task")
def digest_user_task(user_id: int) -> dict:
    """Thin wrapper — the core below is directly callable in tests."""
    return digest_user_core(user_id)


def digest_user_core(user_id: int) -> dict:
    """Send the digest for one tenant (no-op when they have nothing to
    report). SMTP failures are contained to this task."""
    from .digest import send_user_digest

    db = SessionLocal()
    try:
        try:
            return {"sent": send_user_digest(db, user_id)}
        except Exception as exc:  # noqa: BLE001 — per-user isolation
            log.exception("digest send failed for user %d (%s)", user_id, exc)
            db.rollback()
            return {"sent": 0, "error": str(exc)}
    finally:
        db.close()


@celery_app.task(name="app.tasks.auto_archive_tick")
def auto_archive_tick() -> dict:
    """Daily: archive stale jobs — apply_deadline passed, or fetched >14 days
    ago — that are still in new/notified."""
    return auto_archive_tick_core()


def auto_archive_tick_core() -> dict:
    from sqlalchemy import or_

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        stale = now - AUTO_ARCHIVE_STALE
        archived = (db.query(Job)
                    .filter(Job.status.in_(["new", "notified"]),
                            or_(Job.apply_deadline < now, Job.fetched_at < stale))
                    .update({"status": "archived"}, synchronize_session=False))
        db.commit()
        return {"archived": archived}
    finally:
        db.close()


@celery_app.task(name="app.tasks.retention_tick")
def retention_tick() -> dict:
    """Daily 04:11 UTC: hard-delete aged tenant data (see retention_tick_core)."""
    return retention_tick_core()


def retention_tick_core() -> dict:
    """Per-tenant data retention sweep (tenant-safe: every query is scoped by
    user_id):
    - archived jobs fetched >90 days ago — except any still referenced by the
      proposal queue (archived jobs normally have none; referenced ones are
      skipped and counted);
    - done/failed/skipped_circuit_open stealth tasks completed (or, lacking a
      completion stamp, created) >30 days ago;
    - audit_log rows older than 365 days.
    Returns and logs per-category counts.
    """
    from sqlalchemy import func

    from .models import AuditLog, StealthTask

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        jobs_cutoff = now - RETENTION_ARCHIVED_JOB_AGE
        tasks_cutoff = now - RETENTION_STEALTH_TASK_AGE
        audit_cutoff = now - RETENTION_AUDIT_AGE
        totals = {"jobs_deleted": 0, "jobs_skipped_referenced": 0,
                  "stealth_tasks_deleted": 0, "audit_log_deleted": 0}
        for (user_id,) in db.query(User.id).all():
            referenced = (db.query(ProposalQueueItem.job_id)
                          .filter(ProposalQueueItem.user_id == user_id))
            candidates = (db.query(Job)
                          .filter(Job.user_id == user_id,
                                  Job.status == "archived",
                                  Job.fetched_at < jobs_cutoff))
            totals["jobs_skipped_referenced"] += (candidates
                .filter(Job.id.in_(referenced)).count())
            totals["jobs_deleted"] += (candidates
                .filter(~Job.id.in_(referenced))
                .delete(synchronize_session=False))
            totals["stealth_tasks_deleted"] += (
                db.query(StealthTask)
                .filter(StealthTask.user_id == user_id,
                        StealthTask.status.in_(["done", "failed",
                                                "skipped_circuit_open"]),
                        func.coalesce(StealthTask.completed_at,
                                      StealthTask.created_at) < tasks_cutoff)
                .delete(synchronize_session=False))
            totals["audit_log_deleted"] += (db.query(AuditLog)
                .filter(AuditLog.user_id == user_id,
                        AuditLog.created_at < audit_cutoff)
                .delete(synchronize_session=False))
        db.commit()
        log.info("retention sweep: %s", totals)
        return totals
    finally:
        db.close()


@celery_app.task(name="app.tasks.follow_up_due_tick")
def follow_up_due_tick() -> dict:
    """Beat entrypoint — fan-out dispatcher (see follow_up_due_tick_core)."""
    return follow_up_due_tick_core()


def follow_up_due_tick_core() -> dict:
    """Dispatcher: enqueue one follow_up_due_user_task per active user."""
    db = SessionLocal()
    try:
        enqueued = []
        for (user_id,) in db.query(User.id).filter(User.is_active.is_(True)).all():
            try:
                follow_up_due_user_task.delay(user_id)
                enqueued.append(user_id)
            except Exception as exc:  # noqa: BLE001 — broker down; next tick retries
                log.warning("follow-up due dispatch failed for user %d (%s)",
                            user_id, exc)
        return {"enqueued": enqueued}
    finally:
        db.close()


@celery_app.task(name="app.tasks.follow_up_due_user_task")
def follow_up_due_user_task(user_id: int) -> dict:
    """Thin wrapper — the core below is directly callable in tests."""
    return follow_up_due_user_core(user_id)


def follow_up_due_user_core(user_id: int) -> dict:
    """Auto-draft follow-ups for one tenant's unanswered submitted proposals."""
    from .follow_up import generate_due_follow_ups

    db = SessionLocal()
    try:
        user = db.get(User, user_id)
        if user is None:
            return {"queued": [], "skipped": 0}
        try:
            return asyncio.run(generate_due_follow_ups(db, user))
        except Exception as exc:  # noqa: BLE001 — per-user isolation
            log.exception("follow-up due failed for user %d (%s)", user_id, exc)
            db.rollback()
            return {"queued": [], "skipped": 0, "error": str(exc)}
    finally:
        db.close()


@celery_app.task(name="app.tasks.stealth_reaper_tick")
def stealth_reaper_tick() -> dict:
    """Reset stealth tasks stuck in `claimed` (see stealth_reaper_tick_core)."""
    return stealth_reaper_tick_core()


def stealth_reaper_tick_core() -> dict:
    """Reap zombie claims: a worker that crashes mid-task leaves the row
    `claimed` forever — its review-queue item would stay queued_for_browser
    and (for scrape tasks) block outcome sync for that tenant+platform.

    claimed longer than STEALTH_CLAIM_TIMEOUT → back to `pending` (claim
    fields cleared, reclaim_count incremented); once reclaim_count reaches
    STEALTH_MAX_RECLAIMS the task is failed for good.
    """
    from .models import StealthTask

    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - STEALTH_CLAIM_TIMEOUT
        zombies = (db.query(StealthTask)
                   .filter(StealthTask.status == "claimed",
                           StealthTask.claimed_at < cutoff)
                   .all())
        reclaimed, failed = [], []
        for task in zombies:
            if task.reclaim_count < STEALTH_MAX_RECLAIMS:
                task.status = "pending"
                task.claimed_by = None
                task.claimed_at = None
                task.reclaim_count += 1
                reclaimed.append(task.id)
                log.warning("stealth task %d reclaimed (dead worker; reclaim %d/%d)",
                            task.id, task.reclaim_count, STEALTH_MAX_RECLAIMS)
            else:
                task.status = "failed"
                task.result = {"error": "reclaim limit reached (worker died 3 times)"}
                failed.append(task.id)
                log.warning("stealth task %d failed: reclaim limit reached "
                            "(worker died %d times)", task.id, STEALTH_MAX_RECLAIMS)
        db.commit()
        return {"reclaimed": reclaimed, "failed": failed}
    finally:
        db.close()
