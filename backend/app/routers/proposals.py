"""Proposal review queue — the human-in-the-loop compliance boundary.

Proposals are drafted by the orchestrator and park here as pending_review.
Only an explicit approve (with reviewer identity) unlocks submission.
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Job, ProposalQueueItem
from ..schemas import (BulkApproveAction, JobOut, OutcomeAction,
                       ProposalQueueOut, ProposalRejectAction,
                       ProposalReviewAction, TemplateOut)

router = APIRouter(prefix="/api/proposals", tags=["proposals"])
log = logging.getLogger(__name__)


def _with_job(item: ProposalQueueItem, db: Session) -> ProposalQueueOut:
    out = ProposalQueueOut.model_validate(item)
    job = db.get(Job, item.job_id)
    out.job = JobOut.model_validate(job) if job else None
    return out


@router.get("", response_model=list[ProposalQueueOut])
def list_proposals(status: str | None = Query(None), db: Session = Depends(get_db)):
    q = db.query(ProposalQueueItem).order_by(ProposalQueueItem.created_at.desc())
    if status:
        q = q.filter(ProposalQueueItem.status == status)
    return [_with_job(i, db) for i in q.limit(200).all()]


@router.get("/{item_id}", response_model=ProposalQueueOut)
def get_proposal(item_id: int, db: Session = Depends(get_db)):
    item = db.get(ProposalQueueItem, item_id)
    if not item:
        raise HTTPException(404, "proposal not found")
    return _with_job(item, db)


@router.post("/{item_id}/approve", response_model=ProposalQueueOut)
def approve_proposal(item_id: int, body: ProposalReviewAction, db: Session = Depends(get_db)):
    item = db.get(ProposalQueueItem, item_id)
    if not item:
        raise HTTPException(404, "proposal not found")
    if item.status != "pending_review":
        raise HTTPException(409, f"cannot approve a proposal in status '{item.status}'")
    if not body.reviewer:
        raise HTTPException(400, "reviewer identity is required")
    edited = False
    if body.proposal_text is not None and body.proposal_text != item.proposal_text:
        item.proposal_text = body.proposal_text
        edited = True
    if body.bid_amount is not None:
        item.bid_amount = body.bid_amount
    if body.bid_period_days is not None:
        item.bid_period_days = body.bid_period_days
    if edited:
        versions = list(item.versions or [])
        versions.append({"text": item.proposal_text, "bid": item.bid_amount,
                         "by": body.reviewer, "at": datetime.now(timezone.utc).isoformat()})
        item.versions = versions
    item.status = "approved"
    item.reviewed_by = body.reviewer
    item.reviewed_at = datetime.now(timezone.utc)
    from ..models import AuditLog
    from ..templates import save_as_template
    tpl = save_as_template(db, item)
    item.template_id = item.template_id or tpl.id
    db.add(AuditLog(action_type="proposal_approved", platform=item.platform, detail={
        "proposal_id": item.id, "approved_by": body.reviewer, "edited": edited,
        "template_id": item.template_id,
    }))
    db.commit()
    db.refresh(item)
    return _with_job(item, db)


@router.post("/{item_id}/reject", response_model=ProposalQueueOut)
def reject_proposal(item_id: int, body: ProposalRejectAction, db: Session = Depends(get_db)):
    item = db.get(ProposalQueueItem, item_id)
    if not item:
        raise HTTPException(404, "proposal not found")
    if item.status != "pending_review":
        raise HTTPException(409, f"cannot reject a proposal in status '{item.status}'")
    item.status = "rejected"
    item.rejection_reason = body.reason
    item.rejection_notes = body.notes
    item.reviewed_by = body.reviewer
    item.reviewed_at = datetime.now(timezone.utc)
    from ..templates import record_rejection
    record_rejection(db, item, body.reason, body.notes)
    db.commit()
    db.refresh(item)
    return _with_job(item, db)


@router.post("/{item_id}/outcome", response_model=ProposalQueueOut)
def mark_outcome(item_id: int, body: OutcomeAction, db: Session = Depends(get_db)):
    """Mark hired/rejected/ghosted — feeds template win-rate learning."""
    item = db.get(ProposalQueueItem, item_id)
    if not item:
        raise HTTPException(404, "proposal not found")
    from ..templates import record_outcome
    record_outcome(db, item, body.outcome)
    db.refresh(item)
    return _with_job(item, db)


@router.post("/bulk-approve", response_model=dict)
def bulk_approve(body: BulkApproveAction, db: Session = Depends(get_db)):
    """Approve multiple pending proposals at once (reviewer still required)."""
    if not body.reviewer:
        raise HTTPException(400, "reviewer identity is required")
    approved, skipped = [], []
    for pid in body.ids:
        item = db.get(ProposalQueueItem, pid)
        if not item or item.status != "pending_review":
            skipped.append(pid)
            continue
        item.status = "approved"
        item.reviewed_by = body.reviewer
        item.reviewed_at = datetime.now(timezone.utc)
        approved.append(pid)
    db.commit()
    return {"approved": approved, "skipped": skipped}


@router.post("/{item_id}/revert", response_model=ProposalQueueOut)
def revert_version(item_id: int, body: dict, db: Session = Depends(get_db)):
    """Revert to a previous version (by index into `versions`)."""
    item = db.get(ProposalQueueItem, item_id)
    if not item:
        raise HTTPException(404, "proposal not found")
    versions = item.versions or []
    idx = body.get("version_index", 0)
    if not (0 <= idx < len(versions)):
        raise HTTPException(400, f"version_index out of range (0-{len(versions)-1})")
    v = versions[idx]
    item.proposal_text = v.get("text", item.proposal_text)
    if v.get("bid") is not None:
        item.bid_amount = v["bid"]
    db.commit()
    db.refresh(item)
    return _with_job(item, db)


@router.get("/templates/suggest", response_model=list[TemplateOut])
def suggest_templates(platform: str, skills: str = "", db: Session = Depends(get_db)):
    from ..templates import top_templates
    skill_list = [s.strip() for s in skills.split(",") if s.strip()]
    return top_templates(db, platform, skill_list)


@router.post("/templates/generate", response_model=dict)
async def generate_proposal_template(body: dict, db: Session = Depends(get_db)):
    """Generate a reusable proposal Template via the configured text provider
    (Ollama by default). Pass save=true to persist it to the library."""
    from ..models import Template
    from ..proposal_gen import PLATFORM_PROFILES
    from ..textgen import LLMUnavailable, generateText

    platform = body.get("platform", "upwork")
    skills = body.get("skills") or []
    tone = body.get("tone", "")
    profile = PLATFORM_PROFILES.get(platform, PLATFORM_PROFILES["guru"])
    user = (
        f"Write ONE reusable proposal template for {platform} jobs requiring "
        f"skills: {', '.join(skills) or 'general'}. Tone: {tone or 'per your rules'}. "
        "Use the literal tokens {job_title} and {portfolio_links} where job-specific "
        "content would go. Return only the template text."
    )
    offline = False
    warning = None
    try:
        result = await generateText(profile["system"], user,
                                    temperature=body.get("temperature"),
                                    max_tokens=body.get("max_tokens"),
                                    timeout=body.get("timeout"))
        text, model, provider, latency = (result["text"], result["model"],
                                          result["provider"], result["latency_ms"])
    except LLMUnavailable as exc:
        offline, warning = True, str(exc)
        text = (f"Quick note on \"{{job_title}}\" — this is squarely a "
                f"{', '.join(skills) or 'my core stack'} job, and I've shipped similar work.\n\n"
                f"Relevant: {{portfolio_links}}.\n\n"
                f"One question before I lock an estimate: what's the must-have deliverable "
                f"for the first milestone?")
        model, provider, latency = "offline-fallback", "none", 0

    saved = None
    if body.get("save"):
        tpl = Template(title=body.get("title") or f"{platform} template — {', '.join(skills)[:40]}",
                       platform=platform, text=text, tags=skills)
        db.add(tpl)
        db.commit()
        db.refresh(tpl)
        saved = tpl.id
    return {"text": text, "model": model, "provider": provider,
            "latency_ms": latency, "offline": offline, "warning": warning,
            "saved_template_id": saved}


@router.post("/{item_id}/submit", response_model=ProposalQueueOut)
async def submit_proposal(item_id: int, db: Session = Depends(get_db)):
    """Dispatch an APPROVED proposal through the platform's compliant channel.

    freelancer.com → official bid API. upwork → agency-manager queue
    (browser handoff). Other platforms → rejected here; they belong to the
    stealth-browser worker which is out of this service's scope.
    """
    item = db.get(ProposalQueueItem, item_id)
    if not item:
        raise HTTPException(404, "proposal not found")
    if item.status != "approved":
        raise HTTPException(409, "only approved proposals can be submitted (human review boundary)")
    job = db.get(Job, item.job_id)
    if not job:
        raise HTTPException(404, "job not found")

    try:
        if item.platform == "freelancer":
            from ..adapters.freelancer import FreelancerAdapter

            bidder_id = int(item.submission_result.get("bidder_id") or 0)
            if not bidder_id:
                raise HTTPException(
                    400, "submission_result.bidder_id (Freelancer user id) is required"
                )
            adapter = FreelancerAdapter(db)
            try:
                result = await adapter.place_bid(
                    project_id=int(job.external_id),
                    bidder_id=bidder_id,
                    amount=item.bid_amount or 0,
                    period=item.bid_period_days or 7,
                    proposal=item.proposal_text,
                )
            finally:
                await adapter.close()
            item.submission_result = {"channel": "freelancer_api", "response": result}
        elif item.platform == "upwork":
            from ..adapters.upwork_agency import UpworkAgencyAdapter

            adapter = UpworkAgencyAdapter(db)
            record = adapter.submit_proposal(
                job_external_id=job.external_id,
                proposal_text=item.proposal_text,
                on_behalf_of=item.submission_result.get("on_behalf_of", ""),
                connects_required=item.submission_result.get("connects_required", 0),
                approved_by=item.reviewed_by,
            )
            item.submission_result = {"channel": "upwork_agency_queue", "record": record}
        else:
            raise HTTPException(
                501,
                f"submission for '{item.platform}' requires the stealth-browser worker; "
                "leave this item approved and it will be picked up there",
            )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        item.status = "failed"
        item.submission_result = {"error": str(exc)}
        db.commit()
        raise HTTPException(502, f"submission failed: {exc}")

    item.status = "submitted"
    db.commit()
    db.refresh(item)
    return _with_job(item, db)
