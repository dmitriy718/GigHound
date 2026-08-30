"""Follow-up due automation (pull → push).

The daily `follow_up_due_tick` (09:05 UTC) turns the manual
`POST /api/proposals/{id}/follow-up` flow into a push: submitted proposals
that went unanswered for FOLLOW_UP_AFTER_DAYS days get an auto-drafted
follow-up parked in the review queue (status pending_review — the human
boundary is unchanged; nothing is ever sent automatically).

Submission-time proxy: there is no dedicated `submitted_at` column, so the
proxy is `reviewed_at` (set at approval, the last step before submission),
falling back to `created_at`. Upwork items sit in `queued_for_browser` until
the browser worker confirms and flips them to `submitted`, so only truly
submitted items are eligible.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import AuditLog, Job, ProposalQueueItem, User
from .ws_manager import alerts

log = logging.getLogger(__name__)

FOLLOW_UP_AFTER_DAYS = 5
FOLLOW_UP_CAP_PER_RUN = 5


def _submission_proxy():
    """coalesce(reviewed_at, created_at) — the submission-time proxy."""
    return func.coalesce(ProposalQueueItem.reviewed_at,
                         ProposalQueueItem.created_at)


def _due_items(db: Session, user_id: int) -> list[ProposalQueueItem]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=FOLLOW_UP_AFTER_DAYS)
    items = (db.query(ProposalQueueItem)
             .filter(ProposalQueueItem.user_id == user_id,
                     ProposalQueueItem.request_type == "job",
                     ProposalQueueItem.status == "submitted",
                     ProposalQueueItem.outcome == "pending",
                     ProposalQueueItem.client_replied_at.is_(None),
                     _submission_proxy() < cutoff)
             .order_by(ProposalQueueItem.created_at)
             .all())
    if not items:
        return []
    # exclude items that already have ANY follow-up child (even a rejected
    # one — the automation must not nag daily once a human decided)
    parents = {(f.submission_result or {}).get("parent_proposal_id")
               for f in db.query(ProposalQueueItem)
               .filter(ProposalQueueItem.user_id == user_id,
                       ProposalQueueItem.request_type == "follow_up")
               .all()}
    return [i for i in items if i.id not in parents]


async def generate_due_follow_ups(db: Session, user: User) -> dict:
    """Draft follow-ups for one tenant's unanswered submitted proposals.

    Capped per run; per-item failures (LLM/circuit) are logged and skipped so
    one bad item never blocks the rest.
    """
    from . import proposal_gen

    queued, skipped = [], 0
    for item in _due_items(db, user.id)[:FOLLOW_UP_CAP_PER_RUN]:
        try:
            job = db.get(Job, item.job_id)
            if job is None:
                skipped += 1
                continue
            gen = await proposal_gen.generate_follow_up(db, item, job)
            now_iso = datetime.now(timezone.utc).isoformat()
            follow = ProposalQueueItem(
                user_id=user.id, job_id=item.job_id, platform=item.platform,
                request_type="follow_up", status="pending_review",
                proposal_text=gen["humanized_text"] or gen["draft_text"],
                humanized_text=gen["humanized_text"],
                typing_plan=gen["typing_plan"],
                analysis=item.analysis or {},
                portfolio_item_ids=list(item.portfolio_item_ids or []),
                portfolio_match=item.portfolio_match or {},
                confidence=item.confidence,
                needs_review=bool(gen.get("leak_warning")),
                submission_result={
                    "parent_proposal_id": item.id, "auto": True,
                    **({"warning": gen["leak_warning"]} if gen.get("leak_warning") else {})},
                versions=[{"text": gen["draft_text"], "bid": None, "by": "generator",
                           "at": now_iso}],
            )
            db.add(follow)
            db.flush()  # assign id for the audit row
            db.add(AuditLog(user_id=user.id, action_type="follow_up_generated",
                            platform=item.platform, detail={
                                "parent_proposal_id": item.id,
                                "follow_up_id": follow.id,
                                "job_id": item.job_id, "auto": True,
                            }))
            db.commit()
            queued.append(follow.id)
            await alerts.broadcast(user.id, {
                "type": "proposal_queued",
                "proposal_id": follow.id,
                "job": _job_out(job),
            })
        except Exception as exc:  # noqa: BLE001 — per-item isolation (LLM down etc.)
            log.warning("auto follow-up for proposal %d failed (%s); skipped",
                        item.id, exc)
            db.rollback()
            skipped += 1
    return {"queued": queued, "skipped": skipped}


def _job_out(job: Job) -> dict:
    from .schemas import JobOut
    return JobOut.model_validate(job).model_dump(mode="json")
