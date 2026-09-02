"""Browser-platform proposal-status sync via the stealth worker (Advantage
gap: outcome/reply auto-sync was Freelancer-only).

The API platforms (Freelancer) sync through `outcome_sync.py`; the browser
platforms (upwork, fiverr, peopleperhour, guru) have no compliant status API,
so a 60-minute beat enqueues a READ-ONLY `scrape_proposal_status` stealth
task per tenant per platform. The worker loads the proposals/inbox page and
posts per-proposal statuses back to `POST /api/gigs/proposal-status`, which
applies them here:

  hired → outcome hired, declined → rejected (via `templates.record_outcome`,
  so template win rates update — same as the Freelancer path);
  has_unread_reply → `client_replied_at` + `client_replied` WS event.

Everything is idempotent: re-posting the same results is a no-op.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .models import (Job, PlatformAccount, ProposalQueueItem, StealthTask,
                     User)
# canonical set lives in app.platforms
from .platforms import BROWSER_SYNC_PLATFORMS
from .stealth import SCRAPE_PROPOSAL_STATUS, enqueue_stealth_task
from .templates import record_outcome
from .ws_manager import alerts

log = logging.getLogger(__name__)

_WATCHED_STATUSES = ("submitted", "queued_for_browser")

# canonical platform_status → outcome (via record_outcome); everything else
# (pending/viewed/interviewing) leaves the outcome alone
_STATUS_OUTCOME_MAP = {"hired": "hired", "declined": "rejected"}
KNOWN_PLATFORM_STATUSES = ("pending", "viewed", "interviewing",
                           "hired", "declined")


def _enabled_platforms(db: Session, user_id: int) -> set[str]:
    """Browser-sync platforms the tenant has an enabled account for."""
    rows = (db.query(PlatformAccount.platform)
            .filter(PlatformAccount.user_id == user_id,
                    PlatformAccount.platform.in_(BROWSER_SYNC_PLATFORMS),
                    PlatformAccount.enabled.is_(True),
                    PlatformAccount.mode != "disabled")
            .all())
    return {p for (p,) in rows}


def _open_status_task(db: Session, user_id: int,
                      platform: str) -> StealthTask | None:
    """A scrape task still in flight for this tenant+platform (avoids
    stacking dupes; other platforms are unaffected)."""
    return (db.query(StealthTask)
            .filter(StealthTask.user_id == user_id,
                    StealthTask.platform == platform,
                    StealthTask.task_type == SCRAPE_PROPOSAL_STATUS,
                    StealthTask.status.in_(("pending", "claimed")))
            .first())


def enqueue_platform_status_scrapes(db: Session,
                                    user_id: int) -> list[StealthTask]:
    """Enqueue one read-only status-scrape task per browser platform that has
    an enabled account AND open proposals; [] when there is nothing to check
    (or a scrape is already in flight for that platform)."""
    enabled = _enabled_platforms(db, user_id)
    tasks = []
    for platform in BROWSER_SYNC_PLATFORMS:
        if platform not in enabled:
            continue
        items = (db.query(ProposalQueueItem)
                 .filter(ProposalQueueItem.user_id == user_id,
                         ProposalQueueItem.platform == platform,
                         ProposalQueueItem.status.in_(_WATCHED_STATUSES))
                 .all())
        if not items or _open_status_task(db, user_id, platform) is not None:
            continue
        jobs = {j.id: j for j in db.query(Job)
                .filter(Job.id.in_({i.job_id for i in items})).all()}
        checks = []
        for i in items:
            job = jobs.get(i.job_id)
            checks.append({"proposal_queue_item_id": i.id,
                           "job_external_id": job.external_id if job else "",
                           "job_url": job.url if job else ""})
        task = enqueue_stealth_task(db, user_id, platform,
                                    SCRAPE_PROPOSAL_STATUS,
                                    {"items": checks})
        # skipped_circuit_open rows are recorded for UI visibility but will
        # never run — don't report them as enqueued work
        if task.status == "pending":
            tasks.append(task)
    return tasks


async def apply_proposal_status_results(db: Session, task: StealthTask,
                                        results: list[dict]) -> dict:
    """Apply worker-reported statuses to the tenant's queue items.

    Tenancy is enforced per row: a result only lands when the item belongs to
    the task's owner. Idempotent — terminal outcomes and client_replied_at are
    never applied twice (no double win-rate counting, no repeat broadcast).
    """
    outcomes = replies = skipped = 0
    for res in results:
        item = db.get(ProposalQueueItem, res.get("proposal_queue_item_id") or 0)
        if item is None or item.user_id != task.user_id:
            skipped += 1
            continue
        status = (res.get("platform_status") or "").lower()
        if status not in KNOWN_PLATFORM_STATUSES:
            log.warning("proposal-status: unknown status %r for item %d; skipped",
                        status, item.id)
            skipped += 1
            continue
        outcome = _STATUS_OUTCOME_MAP.get(status)
        if outcome and item.outcome == "pending":
            record_outcome(db, item, outcome)
            outcomes += 1
            log.info("proposal-status: proposal %d → %s (task %d)",
                     item.id, outcome, task.id)
        if res.get("has_unread_reply") and item.client_replied_at is None:
            item.client_replied_at = datetime.now(timezone.utc)
            db.commit()
            replies += 1
            await alerts.broadcast(item.user_id, {
                "type": "client_replied",
                "proposal_id": item.id,
                "job_id": item.job_id,
                "snippet": "",
            })
    return {"outcomes": outcomes, "replies": replies, "skipped": skipped}
