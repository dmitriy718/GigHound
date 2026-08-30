"""Proposal review queue — the human-in-the-loop compliance boundary.

Proposals are drafted by the orchestrator and park here as pending_review.
Only an explicit approve (with reviewer identity) unlocks submission.
"""
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..auth import (get_current_user, get_owned, platform_account_settings,
                    platform_enabled, scoped)
from ..database import get_db
from ..models import AuditLog, Job, ProposalQueueItem, Template, User
from ..schemas import (BulkApproveAction, InterviewPrepOut, JobOut, OutcomeAction,
                       ProposalQueueOut, ProposalRejectAction,
                       ProposalReviewAction, TemplateOut)
from ..stealth import SUBMIT_UPWORK_PROPOSAL, enqueue_stealth_task

router = APIRouter(prefix="/api/proposals", tags=["proposals"])
log = logging.getLogger(__name__)


def _with_job(item: ProposalQueueItem, job: Job | None) -> ProposalQueueOut:
    out = ProposalQueueOut.model_validate(item)
    out.job = JobOut.model_validate(job) if job else None
    return out


@router.get("", response_model=dict)
def list_proposals(status: str | None = Query(None),
                   limit: int = Query(50, ge=1, le=200),
                   offset: int = Query(0, ge=0),
                   db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    q = scoped(db, ProposalQueueItem, user)
    if status:
        q = q.filter(ProposalQueueItem.status == status)
    total = q.count()
    items = (q.order_by(ProposalQueueItem.created_at.desc())
             .offset(offset).limit(limit).all())
    # batch-load the page's jobs in one query (no per-item db.get)
    jobs = {}
    if items:
        jobs = {j.id: j for j in db.query(Job)
                .filter(Job.id.in_({i.job_id for i in items})).all()}
    _refresh_bid_advice(db, items, jobs)
    return {"items": [_with_job(i, jobs.get(i.job_id)) for i in items],
            "total": total}


# bid_advice is computed at queue time from the job's proposals_count, which
# keeps moving afterwards — recompute it from the job's CURRENT count when
# the item is older than this, persisting only on change (bounded: the
# returned page only, so list cost stays flat).
_BID_ADVICE_REFRESH_AGE = timedelta(hours=24)


def _refresh_bid_advice(db: Session, items: list[ProposalQueueItem],
                        jobs: dict[int, Job]) -> None:
    from ..client_intel import compute_bid_advice
    now = datetime.now(timezone.utc)
    changed = False
    for item in items:
        ts = item.created_at
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if now - ts < _BID_ADVICE_REFRESH_AGE:
            continue
        job = jobs.get(item.job_id)
        if job is None:
            continue
        advice = compute_bid_advice(job)
        if advice != item.bid_advice:
            item.bid_advice = advice
            changed = True
    if changed:
        db.commit()


@router.get("/{item_id}", response_model=ProposalQueueOut)
def get_proposal(item_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    item = get_owned(db, ProposalQueueItem, item_id, user)
    if not item:
        raise HTTPException(404, "proposal not found")
    return _with_job(item, db.get(Job, item.job_id))


@router.post("/{item_id}/approve", response_model=ProposalQueueOut)
def approve_proposal(item_id: int, body: ProposalReviewAction, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    item = get_owned(db, ProposalQueueItem, item_id, user)
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
    if body.template_id is not None:
        # reviewer picked a suggested template: it must exist and be theirs
        tpl = db.get(Template, body.template_id)
        if tpl is None or tpl.user_id != user.id:
            raise HTTPException(404, "template not found")
        item.template_id = tpl.id
    item.save_as_template = body.save_as_template
    if edited:
        versions = list(item.versions or [])
        versions.append({"text": item.proposal_text, "bid": item.bid_amount,
                         "by": body.reviewer, "at": datetime.now(timezone.utc).isoformat()})
        item.versions = versions
    item.status = "approved"
    item.reviewed_by = body.reviewer
    item.reviewed_at = datetime.now(timezone.utc)
    from ..templates import template_for_approval
    tpl = template_for_approval(db, item)
    item.template_id = item.template_id or (tpl.id if tpl else None)
    db.add(AuditLog(user_id=user.id, action_type="proposal_approved", platform=item.platform, detail={
        "proposal_id": item.id, "approved_by": body.reviewer, "edited": edited,
        "template_id": item.template_id,
    }))
    db.commit()
    db.refresh(item)
    return _with_job(item, db.get(Job, item.job_id))


@router.post("/{item_id}/reject", response_model=ProposalQueueOut)
def reject_proposal(item_id: int, body: ProposalRejectAction, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    item = get_owned(db, ProposalQueueItem, item_id, user)
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
    return _with_job(item, db.get(Job, item.job_id))


@router.post("/{item_id}/outcome", response_model=ProposalQueueOut)
def mark_outcome(item_id: int, body: OutcomeAction, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Mark hired/rejected/ghosted — feeds template win-rate learning."""
    item = get_owned(db, ProposalQueueItem, item_id, user)
    if not item:
        raise HTTPException(404, "proposal not found")
    from ..templates import record_outcome
    record_outcome(db, item, body.outcome)
    db.refresh(item)
    return _with_job(item, db.get(Job, item.job_id))


@router.post("/{item_id}/follow-up", response_model=ProposalQueueOut)
async def draft_follow_up(item_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Draft a follow-up message for a submitted proposal awaiting an outcome.

    Parks a NEW queue item (status pending_review, request_type follow_up) that
    flows through the same human review boundary as any other proposal.
    """
    item = get_owned(db, ProposalQueueItem, item_id, user)
    if not item:
        raise HTTPException(404, "proposal not found")
    if item.status not in ("submitted", "queued_for_browser"):
        raise HTTPException(
            409, f"follow-ups require a submitted proposal (status is '{item.status}')")
    if item.outcome != "pending":
        raise HTTPException(
            409, f"outcome is already '{item.outcome}' — a follow-up no longer makes sense")
    siblings = (scoped(db, ProposalQueueItem, user)
                .filter(ProposalQueueItem.request_type == "follow_up")
                .all())
    if any((s.submission_result or {}).get("parent_proposal_id") == item.id
           and s.status in ("pending_review", "approved") for s in siblings):
        raise HTTPException(409, "a follow-up for this proposal is already pending review")

    job = db.get(Job, item.job_id)
    if not job:
        raise HTTPException(404, "job not found")
    from .. import proposal_gen
    # release the pooled connection before the (potentially 120s) LLM await;
    # nothing is pending, and post-commit attribute access re-acquires briefly
    db.commit()
    gen = await proposal_gen.generate_follow_up(db, item, job)

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
        submission_result={"parent_proposal_id": item.id,
                           **({"warning": gen["leak_warning"]} if gen.get("leak_warning") else {})},
        versions=[{"text": gen["draft_text"], "bid": None, "by": "generator",
                   "at": datetime.now(timezone.utc).isoformat()}],
    )
    db.add(follow)
    db.flush()  # assign id for the audit row
    db.add(AuditLog(user_id=user.id, action_type="follow_up_generated", platform=item.platform, detail={
        "parent_proposal_id": item.id, "follow_up_id": follow.id, "job_id": item.job_id,
    }))
    db.commit()
    db.refresh(follow)
    return _with_job(follow, db.get(Job, follow.job_id))


@router.get("/{item_id}/interview-prep", response_model=InterviewPrepOut)
async def interview_prep(item_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Interview prep sheet from the item's stored analysis + portfolio.

    Cached on the item (submission_result.interview_prep) after the first
    generation — repeated GETs are free.
    """
    item = get_owned(db, ProposalQueueItem, item_id, user)
    if not item:
        raise HTTPException(404, "proposal not found")
    cached = (item.submission_result or {}).get("interview_prep")
    if cached:
        return cached
    job = db.get(Job, item.job_id)
    if not job:
        raise HTTPException(404, "job not found")
    from .. import proposal_gen
    # release the pooled connection before the (potentially 120s) LLM await
    db.commit()
    prep = await proposal_gen.generate_interview_prep(db, item, job)
    item.submission_result = {**(item.submission_result or {}), "interview_prep": prep}
    db.commit()
    return prep


@router.post("/bulk-approve", response_model=dict)
def bulk_approve(body: BulkApproveAction, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Approve multiple pending proposals at once (reviewer still required).

    Each approved item gets the same treatment as a single approve: a
    versions entry, an AuditLog row, and a template snapshot. Items flagged
    `needs_review` (low confidence / output-filter hits) are skipped — bulk
    approval must not wave through drafts the pipeline itself distrusts.
    """
    if not body.reviewer:
        raise HTTPException(400, "reviewer identity is required")
    from ..templates import template_for_approval
    approved, skipped = [], []
    for pid in body.ids:
        item = get_owned(db, ProposalQueueItem, pid, user)
        if not item or item.status != "pending_review" or item.needs_review:
            skipped.append(pid)
            continue
        item.status = "approved"
        item.reviewed_by = body.reviewer
        item.reviewed_at = datetime.now(timezone.utc)
        versions = list(item.versions or [])
        versions.append({"text": item.proposal_text, "bid": item.bid_amount,
                         "by": body.reviewer, "at": datetime.now(timezone.utc).isoformat()})
        item.versions = versions
        tpl = template_for_approval(db, item)
        item.template_id = item.template_id or (tpl.id if tpl else None)
        db.add(AuditLog(user_id=user.id, action_type="proposal_approved", platform=item.platform, detail={
            "proposal_id": item.id, "approved_by": body.reviewer, "edited": False,
            "template_id": item.template_id, "bulk": True,
        }))
        approved.append(pid)
    db.commit()
    return {"approved": approved, "skipped": skipped}


@router.post("/{item_id}/revert", response_model=ProposalQueueOut)
def revert_version(item_id: int, body: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Revert to a previous version (by index into `versions`)."""
    item = get_owned(db, ProposalQueueItem, item_id, user)
    if not item:
        raise HTTPException(404, "proposal not found")
    versions = item.versions or []
    idx = body.get("version_index", 0)
    if not (0 <= idx < len(versions)):
        raise HTTPException(400, f"version_index out of range (0-{len(versions)-1})")
    v = versions[idx]
    changed = False
    new_text = v.get("text", item.proposal_text)
    if new_text != item.proposal_text:
        item.proposal_text = new_text
        changed = True
    if v.get("bid") is not None and v["bid"] != item.bid_amount:
        item.bid_amount = v["bid"]
        changed = True
    if changed:
        # post-approval mutation must re-enter the review boundary
        item.status = "pending_review"
        item.reviewed_by = None
        item.reviewed_at = None
    db.commit()
    db.refresh(item)
    return _with_job(item, db.get(Job, item.job_id))


@router.get("/templates/suggest", response_model=list[TemplateOut])
def suggest_templates(platform: str, skills: str = "", db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from ..templates import top_templates
    skill_list = [s.strip() for s in skills.split(",") if s.strip()]
    return top_templates(db, user.id, platform, skill_list)


@router.post("/templates/generate", response_model=dict)
async def generate_proposal_template(body: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Generate a reusable proposal Template via the configured text provider
    (Ollama by default). Pass save=true to persist it to the library."""
    from ..models import Template
    from ..proposal_gen import PLATFORM_PROFILES
    from ..textgen import LLMUnavailable, generateText

    platform = body.get("platform", "upwork")
    skills = body.get("skills") or []
    tone = body.get("tone", "")
    profile = PLATFORM_PROFILES.get(platform, PLATFORM_PROFILES["guru"])
    prompt = (
        f"Write ONE reusable proposal template for {platform} jobs requiring "
        f"skills: {', '.join(skills) or 'general'}. Tone: {tone or 'per your rules'}. "
        "Use the literal tokens {{job_title}} and {{portfolio_piece}} (double braces) "
        "where job-specific content would go. Return only the template text."
    )
    # release the pooled connection before the (potentially 120s) LLM await —
    # get_current_user already opened a transaction on this session
    db.commit()
    offline = False
    warning = None
    try:
        result = await generateText(profile["system"], prompt,
                                    temperature=body.get("temperature"),
                                    max_tokens=body.get("max_tokens"),
                                    timeout=body.get("timeout"))
        text, model, provider, latency = (result["text"], result["model"],
                                          result["provider"], result["latency_ms"])
    except LLMUnavailable as exc:
        offline, warning = True, str(exc)
        text = (f"Quick note on \"{{job_title}}\" — this is squarely a "
                f"{', '.join(skills) or 'my core stack'} job, and I've shipped similar work.\n\n"
                f"Relevant: {{portfolio_piece}}.\n\n"
                f"One question before I lock an estimate: what's the must-have deliverable "
                f"for the first milestone?")
        model, provider, latency = "offline-fallback", "none", 0

    saved = None
    if body.get("save"):
        tpl = Template(user_id=user.id,
                       title=body.get("title") or f"{platform} template — {', '.join(skills)[:40]}",
                       platform=platform, text=text, tags=skills)
        db.add(tpl)
        db.commit()
        db.refresh(tpl)
        saved = tpl.id
    return {"text": text, "model": model, "provider": provider,
            "latency_ms": latency, "offline": offline, "warning": warning,
            "saved_template_id": saved}


@router.post("/{item_id}/submit", response_model=ProposalQueueOut)
async def submit_proposal(item_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Dispatch an APPROVED proposal through the platform's compliant channel.

    freelancer.com → official bid API. upwork → agency-manager queue
    (browser handoff). Other platforms → rejected here; they belong to the
    stealth-browser worker which is out of this service's scope.
    """
    item = get_owned(db, ProposalQueueItem, item_id, user)
    if not item:
        raise HTTPException(404, "proposal not found")
    if item.status != "approved":
        raise HTTPException(409, "only approved proposals can be submitted (human review boundary)")
    if not platform_enabled(db, user.id, item.platform):
        raise HTTPException(
            409, f"platform '{item.platform}' is disabled — enable it on the Accounts page")
    job = db.get(Job, item.job_id)
    if not job:
        raise HTTPException(404, "job not found")

    channel = ""
    response_id = None
    try:
        if item.platform == "freelancer":
            from ..adapters.freelancer import FreelancerAdapter

            bidder_id = int(item.submission_result.get("bidder_id") or 0)
            if not bidder_id:
                bidder_id = int(platform_account_settings(db, user.id, "freelancer")
                                .get("bidder_id") or 0)
            if not bidder_id:
                raise HTTPException(
                    400, "no Freelancer bidder id: set 'bidder_id' in the freelancer "
                    "account's settings on the Accounts page"
                )
            adapter = FreelancerAdapter(db, user.id)
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
            channel = "freelancer_api"
            response_id = result.get("id")
            # bidder_id rides along so outcome_sync can tell our own messages
            # from client replies (N4)
            item.submission_result = {"channel": channel, "response": result,
                                      "bidder_id": bidder_id}
        elif item.platform == "upwork":
            from ..adapters.upwork_agency import UpworkAgencyAdapter

            on_behalf_of = (item.submission_result.get("on_behalf_of")
                            or platform_account_settings(db, user.id, "upwork")
                            .get("on_behalf_of", ""))
            if not on_behalf_of:
                raise HTTPException(
                    400, "no Upwork agency member: set 'on_behalf_of' in the upwork "
                    "account's settings on the Accounts page"
                )
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
            finally:
                await adapter.close()
            channel = "upwork_agency_queue"
            response_id = record.get("id")
            item.submission_result = {"channel": channel, "record": record}
            # handoff to the stealth-browser worker (AD-4): it executes the
            # agency BM submission and completes this task, which flips the
            # item out of queued_for_browser.
            enqueue_stealth_task(db, user.id, "upwork", SUBMIT_UPWORK_PROPOSAL, {
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

    # Upwork submissions wait for the external browser worker to confirm;
    # only Freelancer bids are truly "submitted" at this point.
    item.status = "submitted" if item.platform == "freelancer" else "queued_for_browser"
    db.add(AuditLog(user_id=user.id, action_type="proposal_submitted", platform=item.platform, detail={
        "proposal_id": item.id, "job_id": job.id, "channel": channel,
        "platform_response_id": response_id, "approved_by": item.reviewed_by,
    }))
    db.commit()
    db.refresh(item)
    return _with_job(item, db.get(Job, item.job_id))
