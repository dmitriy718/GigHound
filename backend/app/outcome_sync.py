"""Outcome + client-reply sync for Freelancer (Phase 2.4).

Polls the Freelancer adapter for submitted (or browser-queued) proposals:
  * `get_bid_status` → awarded ⇒ hired, rejected ⇒ rejected (via
    `templates.record_outcome`, so template win rates update);
  * `get_threads` → a client message newer than the submission sets
    `client_replied_at` and pushes a `client_replied` WS event.

Per-item errors are logged and skipped — one bad bid never kills the tick.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from .adapters.base import AdapterError
from .adapters.freelancer import FreelancerAdapter
from .models import AdapterCredential, Job, ProposalQueueItem, User
from .templates import record_outcome
from .ws_manager import alerts

log = logging.getLogger(__name__)

_BID_OUTCOME_MAP = {"awarded": "hired", "rejected": "rejected"}
_WATCHED_STATUSES = ("submitted", "queued_for_browser")


def _has_freelancer_credentials(db: Session, user_id: int) -> bool:
    return (db.query(AdapterCredential)
            .filter(AdapterCredential.user_id == user_id,
                    AdapterCredential.platform == "freelancer")
            .first()) is not None


def _bid_id_of(item: ProposalQueueItem) -> int | None:
    result = item.submission_result or {}
    bid_id = (result.get("response") or {}).get("id") or result.get("bid_id")
    try:
        return int(bid_id) if bid_id is not None else None
    except (TypeError, ValueError):
        return None


def _bidder_id_of(item: ProposalQueueItem) -> int | None:
    """Our Freelancer user id for this bid (to tell client messages from ours)."""
    try:
        bidder_id = (item.submission_result or {}).get("bidder_id")
        return int(bidder_id) if bidder_id else None
    except (TypeError, ValueError):
        return None


def _submitted_at(item: ProposalQueueItem) -> datetime:
    ts = item.reviewed_at or item.created_at
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _client_reply(thread: dict, item: ProposalQueueItem, bidder_id: int | None,
                  job: Job) -> tuple[float, str] | None:
    """(timestamp, snippet) when the thread holds a client message newer than
    the submission for this item's project; else None."""
    project = thread.get("project") or {}
    project_id = project.get("id") or thread.get("project_id")
    if str(project_id) != str(job.external_id):
        return None
    last = thread.get("last_message") or {}
    from_user = last.get("from_user")
    if bidder_id and from_user == bidder_id:
        return None  # our own message
    ts = last.get("time")
    if not ts:
        return None
    if datetime.fromtimestamp(ts, timezone.utc) <= _submitted_at(item):
        return None
    return ts, (last.get("message") or "")[:200]


async def sync_user_outcomes(db: Session, user: User) -> dict:
    """Poll bid statuses + message threads for one tenant's open proposals."""
    items = (db.query(ProposalQueueItem)
             .filter(ProposalQueueItem.user_id == user.id,
                     ProposalQueueItem.platform == "freelancer",
                     ProposalQueueItem.status.in_(_WATCHED_STATUSES))
             .all())
    if not items or not _has_freelancer_credentials(db, user.id):
        return {"checked": 0, "outcomes": 0, "replies": 0}

    adapter = FreelancerAdapter(db, user.id)
    outcomes = replies = 0
    try:
        threads: list[dict] = []
        threads_fetched = False
        for item in items:
            try:
                job = db.get(Job, item.job_id)
                if job is None:
                    continue
                bid_id = _bid_id_of(item)
                if bid_id and item.outcome == "pending":
                    status = await adapter.get_bid_status(bid_id)
                    outcome = _BID_OUTCOME_MAP.get((status.get("status") or "").lower())
                    if outcome:
                        record_outcome(db, item, outcome)
                        outcomes += 1
                        log.info("outcome sync: proposal %d → %s", item.id, outcome)

                if item.client_replied_at is None:
                    if not threads_fetched:
                        threads = await adapter.get_threads()
                        threads_fetched = True
                    bidder_id = _bidder_id_of(item)
                    for thread in threads:
                        hit = _client_reply(thread, item, bidder_id, job)
                        if hit:
                            ts, snippet = hit
                            item.client_replied_at = datetime.fromtimestamp(
                                ts, timezone.utc)
                            db.commit()
                            replies += 1
                            await alerts.broadcast(user.id, {
                                "type": "client_replied",
                                "proposal_id": item.id,
                                "job_id": item.job_id,
                                "snippet": snippet,
                            })
                            break
            except Exception as exc:  # noqa: BLE001 — per-item isolation
                log.warning("outcome sync: item %d failed (%s); continuing",
                            item.id, exc)
                db.rollback()
    except AdapterError as exc:
        log.warning("outcome sync: adapter failed for user %d: %s", user.id, exc)
    finally:
        await adapter.close()
    return {"checked": len(items), "outcomes": outcomes, "replies": replies}
