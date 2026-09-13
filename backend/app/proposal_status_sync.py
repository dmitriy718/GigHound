"""Browser-platform proposal-status sync via the stealth worker (Advantage
gap: outcome/reply auto-sync was Freelancer-only).

The API platforms (Freelancer) sync through `outcome_sync.py`; the browser
platforms (upwork, fiverr, peopleperhour, guru) have no compliant status API,
so a 60-minute beat enqueues a READ-ONLY `scrape_proposal_status` stealth
task per tenant, platform and account. The worker loads the proposals/inbox page and
posts per-proposal statuses back to `POST /api/gigs/proposal-status`, which
applies them here:

  hired → outcome hired, declined → rejected (via `templates.record_outcome`,
  so template win rates update — same as the Freelancer path);
  has_unread_reply → `client_replied_at` + `client_replied` WS event.

Everything is idempotent: re-posting the same results is a no-op.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import or_
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
                      platform: str, account_id: int | None = None) -> StealthTask | None:
    """A scrape task still in flight for this tenant+platform (avoids
    stacking dupes; other platforms are unaffected)."""
    return (db.query(StealthTask)
            .filter(StealthTask.user_id == user_id,
                    StealthTask.platform == platform,
                    StealthTask.task_type == SCRAPE_PROPOSAL_STATUS,
                    *([or_(StealthTask.payload["account_id"].as_integer() == account_id,
                           StealthTask.payload["account_id"].as_integer().is_(None))] if account_id is not None else []),
                    StealthTask.status.in_(("pending", "claimed")))
            .first())


def enqueue_platform_status_scrapes(db: Session,
                                    user_id: int) -> list[StealthTask]:
    """Group watched proposals by reviewed browser account, refusing ambiguity."""
    tasks = []
    for platform in BROWSER_SYNC_PLATFORMS:
        accounts = db.query(PlatformAccount).filter(
            PlatformAccount.user_id == user_id, PlatformAccount.platform == platform,
            PlatformAccount.enabled.is_(True), PlatformAccount.mode.in_(['stealth','hybrid'])
        ).order_by(PlatformAccount.id).all()
        if not accounts:
            continue
        items = db.query(ProposalQueueItem).filter(
            ProposalQueueItem.user_id == user_id, ProposalQueueItem.platform == platform,
            ProposalQueueItem.status.in_(_WATCHED_STATUSES)).all()
        jobs = {j.id: j for j in db.query(Job).filter(
            Job.user_id == user_id, Job.id.in_({i.job_id for i in items})).all()} if items else {}
        groups = {a.id: [] for a in accounts}
        for item in items:
            account_id = (item.approved_snapshot or {}).get('account_id') or item.platform_account_id
            if account_id is None and len(accounts) == 1:
                account_id = accounts[0].id
            if account_id not in groups:
                log.warning('status sync: proposal %d needs account reconciliation',item.id)
                continue
            job = jobs.get(item.job_id)
            if job:
                groups[account_id].append({'proposal_queue_item_id':item.id,
                                          'job_external_id':job.external_id,'job_url':job.url})
        for account_id, checks in groups.items():
            if not checks or _open_status_task(db,user_id,platform,account_id) is not None:
                continue
            task = enqueue_stealth_task(db,user_id,platform,SCRAPE_PROPOSAL_STATUS,
                                       {'account_id':account_id,'items':checks})
            if task.status == 'pending':
                tasks.append(task)
    return tasks


async def apply_proposal_status_results(db: Session, task: StealthTask,
                                        results: list[dict], notifications: list | None = None) -> dict:
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
        if outcome and item.outcome == "pending" and item.status == "submitted":
            record_outcome(db, item, outcome, commit=False)
            outcomes += 1
            log.info("proposal-status: proposal %d → %s (task %d)",
                     item.id, outcome, task.id)
        if res.get("has_unread_reply") and item.client_replied_at is None:
            item.client_replied_at = datetime.now(timezone.utc)
            db.flush()
            replies += 1
            if notifications is not None:
                notifications.append((item.user_id, {
                    "type": "client_replied", "proposal_id": item.id,
                    "job_id": item.job_id, "snippet": "",
                }))
    return {"outcomes": outcomes, "replies": replies, "skipped": skipped}
