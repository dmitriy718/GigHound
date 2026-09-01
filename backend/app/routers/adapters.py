"""Adapter control endpoints: discovery → ingest bridge, and gated write actions.

Write actions (bid placement, Upwork proposal queueing) are bound to the
human review queue: they take a `proposal_queue_item_id` in `approved`
status and send exactly the queued text/bid — no caller-supplied content.
"""
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..adapters.base import AdapterError, QuotaDepletedError
from ..adapters.freelancer import FreelancerAdapter
from ..adapters.linkedin import LinkedInJobsAdapter
from ..adapters.upwork_agency import UpworkAgencyAdapter
from ..auth import (get_current_user, get_owned, platform_account_settings,
                    platform_enabled)
from ..database import get_db
from ..schemas import IngestJobsIn, JobOut
from ..models import AuditLog, Job, ProposalQueueItem, User
from ..ingest import run_ingest  # reuse scoring/alert pipeline
from ..stealth import SUBMIT_UPWORK_PROPOSAL, enqueue_stealth_task

router = APIRouter(prefix="/api/adapters", tags=["adapters"])


class SearchRequest(BaseModel):
    query: str = ""
    limit: int = 25
    location: str = ""
    remote_only: bool = True
    sandbox: bool = False
    auto_ingest: bool = True


class QueueItemAction(BaseModel):
    proposal_queue_item_id: int


def _load_approved_item(db: Session, user: User, item_id: int) -> ProposalQueueItem:
    """The review-queue binding for write actions: owned by the caller and approved."""
    item = get_owned(db, ProposalQueueItem, item_id, user)
    if not item:
        raise HTTPException(404, "proposal queue item not found")
    if item.status != "approved":
        raise HTTPException(
            409, f"queue item is '{item.status}'; only approved items can be sent "
            "(human review boundary)"
        )
    return item


def _require_platform_enabled(db: Session, user: User, platform: str) -> None:
    """Kill switch: a disabled PlatformAccount stops all automation for it."""
    if not platform_enabled(db, user.id, platform):
        raise HTTPException(
            409, f"platform '{platform}' is disabled — enable it on the Accounts page")


@router.post("/freelancer/search", response_model=dict)
async def freelancer_search(body: SearchRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_platform_enabled(db, user, "freelancer")
    adapter = FreelancerAdapter(db, user.id, sandbox=body.sandbox)
    try:
        postings = await adapter.search_jobs(body.query, limit=body.limit)
        ingested = None
        if body.auto_ingest:
            result = await run_ingest(IngestJobsIn(jobs=[p.to_ingest() for p in postings]), db, user)
            ingested = result.model_dump()
        return {
            "found": len(postings),
            "ingest": ingested,
            "jobs": [p.model_dump(mode="json", exclude={"raw_data"}) for p in postings],
        }
    except AdapterError as exc:
        raise HTTPException(502, str(exc))
    finally:
        await adapter.close()


@router.post("/freelancer/bid", response_model=dict)
async def freelancer_bid(body: QueueItemAction, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Place a bid for an APPROVED review-queue item.

    The proposal text and bid amount come from the queue item, never from
    the caller — this endpoint cannot bypass the human review boundary.
    """
    item = _load_approved_item(db, user, body.proposal_queue_item_id)
    _require_platform_enabled(db, user, "freelancer")
    job = db.get(Job, item.job_id)
    if not job:
        raise HTTPException(404, "job not found")
    bidder_id = int(item.submission_result.get("bidder_id") or 0)
    if not bidder_id:
        bidder_id = int(platform_account_settings(db, user.id, "freelancer")
                        .get("bidder_id") or 0)
    if not bidder_id:
        raise HTTPException(400, "no Freelancer bidder id: set 'bidder_id' in the "
                            "freelancer account's settings on the Accounts page")
    adapter = FreelancerAdapter(db, user.id)
    try:
        result = await adapter.place_bid(
            project_id=int(job.external_id), bidder_id=bidder_id,
            amount=item.bid_amount or 0, period=item.bid_period_days or 7,
            proposal=item.proposal_text,
        )
    except QuotaDepletedError as exc:
        raise HTTPException(429, str(exc))
    except AdapterError as exc:
        raise HTTPException(502, str(exc))
    finally:
        await adapter.close()
    db.add(AuditLog(user_id=user.id, action_type="bid_placed", platform="freelancer", detail={
        "proposal_queue_item_id": item.id, "project_id": job.external_id,
        "approved_by": item.reviewed_by, "response_id": result.get("id"),
    }))
    db.commit()
    return {"bid": result, "bids_remaining": adapter.bids_remaining()}


@router.get("/freelancer/quota", response_model=dict)
def freelancer_quota(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    adapter = FreelancerAdapter(db, user.id)
    return {"monthly_quota": adapter.monthly_bid_quota, "bids_remaining": adapter.bids_remaining()}


@router.post("/upwork/search", response_model=dict)
async def upwork_search(body: SearchRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_platform_enabled(db, user, "upwork")
    adapter = UpworkAgencyAdapter(db, user.id)
    try:
        postings = await adapter.search_jobs(body.query, limit=body.limit)
        ingested = None
        if body.auto_ingest:
            result = await run_ingest(IngestJobsIn(jobs=[p.to_ingest() for p in postings]), db, user)
            ingested = result.model_dump()
        return {
            "found": len(postings),
            "ingest": ingested,
            "jobs": [p.model_dump(mode="json", exclude={"raw_data"}) for p in postings],
        }
    except AdapterError as exc:
        raise HTTPException(502, str(exc))
    finally:
        await adapter.close()


@router.post("/upwork/proposals", response_model=dict)
def upwork_submit_proposal(body: QueueItemAction, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Queue an Upwork agency-manager submission for an APPROVED review-queue item.

    Sends exactly the queued proposal text (Upwork 2026 AI policy: the
    approval comes from the review queue, not from a caller-supplied string).
    """
    item = _load_approved_item(db, user, body.proposal_queue_item_id)
    _require_platform_enabled(db, user, "upwork")
    job = db.get(Job, item.job_id)
    if not job:
        raise HTTPException(404, "job not found")
    on_behalf_of = (item.submission_result.get("on_behalf_of")
                    or platform_account_settings(db, user.id, "upwork")
                    .get("on_behalf_of", ""))
    if not on_behalf_of:
        raise HTTPException(400, "no Upwork agency member: set 'on_behalf_of' in the "
                            "upwork account's settings on the Accounts page")
    connects_required = item.submission_result.get("connects_required", 0)
    adapter = UpworkAgencyAdapter(db, user.id)
    try:
        record = adapter.submit_proposal(
            job_external_id=job.external_id,
            proposal_text=item.proposal_text,
            on_behalf_of=on_behalf_of,
            connects_required=connects_required,
            approved_by=item.reviewed_by,
        )
    except AdapterError as exc:
        raise HTTPException(400, str(exc))
    # handoff to the stealth-browser worker (AD-4)
    stealth_task = enqueue_stealth_task(db, user.id, "upwork", SUBMIT_UPWORK_PROPOSAL, {
        "job_external_id": job.external_id,
        "job_url": job.url,
        "proposal_text": item.proposal_text,
        "humanized_text": item.humanized_text or item.proposal_text,
        "typing_plan": item.typing_plan or [],
        "on_behalf_of": on_behalf_of,
        "connects_required": connects_required,
        "bid_amount": item.bid_amount,
        "proposal_queue_item_id": item.id,
    })
    if stealth_task is None or stealth_task.status == "skipped_circuit_open":
        # circuit open: no worker will ever run this task — leave the item
        # approved so it stays submittable once the circuit closes
        reason = ((stealth_task.result or {}).get("reason", "")
                  if stealth_task is not None else "")
        if not reason:
            from .. import circuit_breaker
            reason = circuit_breaker.check("upwork", user.id)[1] or "upwork circuit is open"
        raise HTTPException(409, reason)
    # same status contract as routers/proposals.py submit: task completion
    # (gigs.py _apply_submission_outcome) only flips queued_for_browser items
    item.status = "queued_for_browser"
    db.add(AuditLog(user_id=user.id, action_type="proposal_queued", platform="upwork", detail={
        "proposal_queue_item_id": item.id, "job_external_id": job.external_id,
        "approved_by": item.reviewed_by, "record_status": record.get("status"),
    }))
    db.commit()
    return {"queued": record}


@router.get("/upwork/agency/members", response_model=dict)
def upwork_agency_members(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return {"members": UpworkAgencyAdapter(db, user.id).list_agency_members()}


@router.post("/upwork/agency/members", response_model=dict)
def upwork_agency_add_member(body: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    username = body.get("username")
    if not username:
        raise HTTPException(400, "username required")
    return {"members": UpworkAgencyAdapter(db, user.id).add_agency_member(username)}


@router.delete("/upwork/agency/members/{username}", response_model=dict)
def upwork_agency_remove_member(username: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return {"members": UpworkAgencyAdapter(db, user.id).remove_agency_member(username)}


@router.post("/linkedin/search", response_model=dict)
async def linkedin_search(body: SearchRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_platform_enabled(db, user, "linkedin")
    provider = os.getenv("LINKEDIN_PROVIDER", "theirstack")
    adapter = LinkedInJobsAdapter(db, user_id=user.id, provider=provider)
    try:
        postings = await adapter.search_jobs(
            body.query, location=body.location, remote_only=body.remote_only, limit=body.limit
        )
        ingested = None
        if body.auto_ingest:
            result = await run_ingest(IngestJobsIn(jobs=[p.to_ingest() for p in postings]), db, user)
            ingested = result.model_dump()
        return {
            "found": len(postings),
            "ingest": ingested,
            "jobs": [p.model_dump(mode="json", exclude={"raw_data"}) for p in postings],
        }
    except AdapterError as exc:
        raise HTTPException(502, str(exc))
    finally:
        await adapter.close()
