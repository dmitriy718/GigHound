from ..schemas import AgencyMemberIn
"""Adapter control endpoints: discovery → ingest bridge, and gated write actions.

Write actions (bid placement, Upwork proposal queueing) are bound to the
human review queue: they take a `proposal_queue_item_id` in `approved`
status and send exactly the queued text/bid — no caller-supplied content.
"""
import logging
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..adapters.base import AdapterError
from ..adapters.accounts import selected_principal
from ..adapters.freelancer import FreelancerAdapter
from ..adapters.linkedin import LinkedInJobsAdapter
from ..adapters.upwork_agency import UpworkAgencyAdapter
from ..auth import get_current_user, get_owned, platform_enabled
from ..database import get_db
from ..schemas import IngestJobsIn, JobOut
from ..models import ProposalQueueItem, User
from ..ingest import run_ingest  # reuse scoring/alert pipeline

router = APIRouter(prefix="/api/adapters", tags=["adapters"])
log = logging.getLogger(__name__)


class SearchRequest(BaseModel):
    account_id: int | None = Field(default=None, ge=1)
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
    adapter = FreelancerAdapter(db, user.id, sandbox=body.sandbox, principal=selected_principal(db, user.id, "freelancer", body.account_id, "default"))
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
        # AdapterError messages can embed upstream API bodies/URLs — log the
        # detail server-side, return generic text to the client
        log.warning("adapter call failed for user %d: %s", user.id, exc)
        raise HTTPException(502, "upstream request failed")
    finally:
        await adapter.close()


@router.post("/freelancer/bid", response_model=dict)
async def freelancer_bid(body: QueueItemAction, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Compatibility route using the same atomic dispatch as the review queue."""
    item = _load_approved_item(db, user, body.proposal_queue_item_id)
    if item.platform != "freelancer":
        raise HTTPException(409, "proposal belongs to another platform")
    from .proposals import submit_proposal
    result = await submit_proposal(item.id, db, user)
    adapter = FreelancerAdapter(db, user.id, principal=selected_principal(db, user.id, "freelancer", (item.approved_snapshot or {}).get("account_id"), "default"))
    try:
        return {"bid": result.submission_result.get("response", {}),
                "bids_remaining": adapter.bids_remaining()}
    finally:
        await adapter.close()


@router.get("/freelancer/quota", response_model=dict)
async def freelancer_quota(db: Session = Depends(get_db), user: User = Depends(get_current_user), account_id: int | None = None):
    adapter = FreelancerAdapter(db, user.id, principal=selected_principal(db, user.id, "freelancer", account_id, "default"))
    try:
        return {"monthly_quota": adapter.monthly_bid_quota, "bids_remaining": adapter.bids_remaining()}
    finally:
        await adapter.close()


@router.get("/freelancer/sync-status", response_model=dict)
def freelancer_sync_status(db: Session = Depends(get_db), user: User = Depends(get_current_user), account_id: int | None = None):
    from ..outcome_sync import reply_cursor_key
    from ..adapters.vault import StateStore
    principal = selected_principal(db,user.id,'freelancer',account_id,'default')
    return {'account_id':account_id,'reply_polling':StateStore(db,user.id).get('freelancer',reply_cursor_key(principal),{}),
            'requires_known_client_id':True}


@router.post("/upwork/search", response_model=dict)
async def upwork_search(body: SearchRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_platform_enabled(db, user, "upwork")
    adapter = UpworkAgencyAdapter(db, user.id, principal=selected_principal(db, user.id, "upwork", body.account_id, "agency_manager"))
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
        # AdapterError messages can embed upstream API bodies/URLs — log the
        # detail server-side, return generic text to the client
        log.warning("adapter call failed for user %d: %s", user.id, exc)
        raise HTTPException(502, "upstream request failed")
    finally:
        await adapter.close()


@router.post("/upwork/proposals", response_model=dict)
async def upwork_submit_proposal(body: QueueItemAction, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Compatibility route using the same atomic dispatch as the review queue."""
    item = _load_approved_item(db, user, body.proposal_queue_item_id)
    if item.platform != "upwork":
        raise HTTPException(409, "proposal belongs to another platform")
    from .proposals import submit_proposal
    result = await submit_proposal(item.id, db, user)
    return {"queued": result.submission_result.get("record", {})}


@router.get("/upwork/agency/legacy-roster", response_model=dict)
def legacy_agency_roster(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from ..models import AdapterState
    row = db.query(AdapterState).filter_by(user_id=user.id,platform='upwork',key='agency_roster').first()
    return {'members':(row.value or {}).get('members',[]) if row else []}


@router.post("/upwork/agency/legacy-roster/assign", response_model=dict)
def assign_legacy_agency_roster(account_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    import copy, hashlib
    from ..models import AdapterState, AuditLog
    db.refresh(user,with_for_update=True)
    principal = selected_principal(db,user.id,'upwork',account_id,'agency_manager')
    legacy = db.query(AdapterState).filter_by(user_id=user.id,platform='upwork',key='agency_roster').populate_existing().first()
    if legacy is None:
        raise HTTPException(409,'no unassigned legacy roster remains')
    value = legacy.value
    members = value.get('members') if isinstance(value,dict) else None
    if not isinstance(members,list) or any(not isinstance(m,dict) or not isinstance(m.get('username'),str) for m in members):
        raise HTTPException(409,'legacy roster needs manual data repair before assignment')
    key='agency_roster:'+hashlib.sha256(principal.encode()).hexdigest()
    target = db.query(AdapterState).filter_by(user_id=user.id,platform='upwork',key=key).populate_existing().first()
    if target is not None and (target.value or {}).get('members'):
        raise HTTPException(409,'selected account already has a roster; reconcile it before assigning legacy members')
    if target is None:
        target=AdapterState(user_id=user.id,platform='upwork',key=key)
        db.add(target)
    target.value=copy.deepcopy(value)
    db.delete(legacy)
    db.add(AuditLog(user_id=user.id,platform='upwork',action_type='legacy_roster_assigned',detail={'account_id':account_id,'members':len(members)}))
    db.commit()
    return {'members':members}


@router.get("/upwork/agency/members", response_model=dict)
async def upwork_agency_members(db: Session = Depends(get_db), user: User = Depends(get_current_user), account_id: int | None = None):
    adapter = UpworkAgencyAdapter(db, user.id, principal=selected_principal(db, user.id, "upwork", account_id, "agency_manager"))
    try:
        return {"members": adapter.list_agency_members()}
    finally:
        await adapter.close()


@router.post("/upwork/agency/members", response_model=dict)
async def upwork_agency_add_member(body: AgencyMemberIn, db: Session = Depends(get_db), user: User = Depends(get_current_user), account_id: int | None = None):
    adapter = UpworkAgencyAdapter(db, user.id, principal=selected_principal(db, user.id, "upwork", account_id, "agency_manager"))
    try:
        return {"members": adapter.add_agency_member(body.username)}
    finally:
        await adapter.close()


@router.delete("/upwork/agency/members/{username}", response_model=dict)
async def upwork_agency_remove_member(username: str, db: Session = Depends(get_db), user: User = Depends(get_current_user), account_id: int | None = None):
    adapter = UpworkAgencyAdapter(db, user.id, principal=selected_principal(db, user.id, "upwork", account_id, "agency_manager"))
    try:
        return {"members": adapter.remove_agency_member(username)}
    finally:
        await adapter.close()


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
        # AdapterError messages can embed upstream API bodies/URLs — log the
        # detail server-side, return generic text to the client
        log.warning("adapter call failed for user %d: %s", user.id, exc)
        raise HTTPException(502, "upstream request failed")
    finally:
        await adapter.close()
