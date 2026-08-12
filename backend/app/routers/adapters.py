"""Adapter control endpoints: discovery → ingest bridge, and gated write actions.

Write actions (bid placement, Upwork proposal queueing) REQUIRE an explicit
`approved_by` — the human review queue boundary is enforced here as well.
"""
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..adapters.base import AdapterError, QuotaDepletedError
from ..adapters.freelancer import FreelancerAdapter
from ..adapters.linkedin import LinkedInJobsAdapter
from ..adapters.upwork_agency import UpworkAgencyAdapter
from ..database import get_db
from ..schemas import JobOut
from ..models import Job
from .jobs import ingest_jobs  # reuse scoring/alert pipeline

router = APIRouter(prefix="/api/adapters", tags=["adapters"])


class SearchRequest(BaseModel):
    query: str = ""
    limit: int = 25
    location: str = ""
    remote_only: bool = True
    sandbox: bool = False
    auto_ingest: bool = True


class BidRequest(BaseModel):
    project_id: int
    bidder_id: int
    amount: float
    period: int
    proposal: str
    milestone_percentage: int | None = None
    approved_by: str  # human review queue approver — required


class ProposalRequest(BaseModel):
    job_external_id: str
    proposal_text: str
    on_behalf_of: str
    connects_required: int = 0
    approved_by: str  # required by Upwork 2026 AI policy


@router.post("/freelancer/search", response_model=dict)
async def freelancer_search(body: SearchRequest, db: Session = Depends(get_db)):
    adapter = FreelancerAdapter(db, sandbox=body.sandbox)
    try:
        postings = await adapter.search_jobs(body.query, limit=body.limit)
        ingested = None
        if body.auto_ingest:
            result = await ingest_jobs({"jobs": [p.to_ingest().model_dump(mode="json") for p in postings]}, db)
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
async def freelancer_bid(body: BidRequest, db: Session = Depends(get_db)):
    if not body.approved_by:
        raise HTTPException(400, "approved_by is required (human review boundary)")
    adapter = FreelancerAdapter(db)
    try:
        result = await adapter.place_bid(
            project_id=body.project_id, bidder_id=body.bidder_id, amount=body.amount,
            period=body.period, proposal=body.proposal,
            milestone_percentage=body.milestone_percentage,
        )
        return {"bid": result, "bids_remaining": adapter.bids_remaining()}
    except QuotaDepletedError as exc:
        raise HTTPException(429, str(exc))
    except AdapterError as exc:
        raise HTTPException(502, str(exc))
    finally:
        await adapter.close()


@router.get("/freelancer/quota", response_model=dict)
def freelancer_quota(db: Session = Depends(get_db)):
    adapter = FreelancerAdapter(db)
    return {"monthly_quota": adapter.monthly_bid_quota, "bids_remaining": adapter.bids_remaining()}


@router.post("/upwork/search", response_model=dict)
async def upwork_search(body: SearchRequest, db: Session = Depends(get_db)):
    adapter = UpworkAgencyAdapter(db)
    try:
        postings = await adapter.search_jobs(body.query, limit=body.limit)
        ingested = None
        if body.auto_ingest:
            result = await ingest_jobs({"jobs": [p.to_ingest().model_dump(mode="json") for p in postings]}, db)
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
def upwork_submit_proposal(body: ProposalRequest, db: Session = Depends(get_db)):
    adapter = UpworkAgencyAdapter(db)
    try:
        record = adapter.submit_proposal(
            job_external_id=body.job_external_id,
            proposal_text=body.proposal_text,
            on_behalf_of=body.on_behalf_of,
            connects_required=body.connects_required,
            approved_by=body.approved_by,
        )
        return {"queued": record}
    except AdapterError as exc:
        raise HTTPException(400, str(exc))


@router.get("/upwork/agency/members", response_model=dict)
def upwork_agency_members(db: Session = Depends(get_db)):
    return {"members": UpworkAgencyAdapter(db).list_agency_members()}


@router.post("/upwork/agency/members", response_model=dict)
def upwork_agency_add_member(body: dict, db: Session = Depends(get_db)):
    username = body.get("username")
    if not username:
        raise HTTPException(400, "username required")
    return {"members": UpworkAgencyAdapter(db).add_agency_member(username)}


@router.delete("/upwork/agency/members/{username}", response_model=dict)
def upwork_agency_remove_member(username: str, db: Session = Depends(get_db)):
    return {"members": UpworkAgencyAdapter(db).remove_agency_member(username)}


@router.post("/linkedin/search", response_model=dict)
async def linkedin_search(body: SearchRequest, db: Session = Depends(get_db)):
    provider = os.getenv("LINKEDIN_PROVIDER", "theirstack")
    adapter = LinkedInJobsAdapter(db, provider=provider)
    try:
        postings = await adapter.search_jobs(
            body.query, location=body.location, remote_only=body.remote_only, limit=body.limit
        )
        ingested = None
        if body.auto_ingest:
            result = await ingest_jobs({"jobs": [p.to_ingest().model_dump(mode="json") for p in postings]}, db)
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
