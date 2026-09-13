from ..schemas import TemplateGenerateIn
"""Proposal review queue — the human-in-the-loop compliance boundary.

Proposals are drafted by the orchestrator and park here as pending_review.
Only an explicit approve (with reviewer identity) unlocks submission.
"""
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import update
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from ..auth import (get_current_user, get_owned,
                    platform_enabled, scoped)
from ..database import get_db
from ..models import AuditLog, Job, PlatformAccount, ProposalQueueItem, Template, User
from ..ratelimit import check_llm_gen_rate
from ..schemas import (BulkApproveAction, InterviewPrepOut, JobOut, MarkSubmittedAction,
                       OutcomeAction, ProposalQueueOut, ProposalRejectAction,
                       ProposalReviewAction, TemplateOut)
from ..schemas import SubmissionReconcileIn
from ..stealth import SUBMIT_FIVERR_OFFER, SUBMIT_UPWORK_PROPOSAL, enqueue_stealth_task

router = APIRouter(prefix="/api/proposals", tags=["proposals"])
log = logging.getLogger(__name__)


from pydantic import BaseModel, Field


class TonePreviewIn(BaseModel):
    expected_revision: int = Field(ge=1)


@router.post("/{item_id}/tone-preview")
async def preview_application_tone(item_id: int, body: TonePreviewIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from .. import proposal_gen
    item = get_owned(db, ProposalQueueItem, item_id, user)
    if item is None:
        raise HTTPException(404, "proposal not found")
    if item.status != "pending_review" or item.revision != body.expected_revision:
        raise HTTPException(409, "Reload and review the current pending proposal before rewriting.")
    check_llm_gen_rate(user)
    job = get_owned(db, Job, item.job_id, user)
    if job is None:
        raise HTTPException(404, "job not found")
    # Return a new reviewable preview; never overwrite a saved or approved version.
    result = (await proposal_gen.generate_follow_up(db, item, job) if item.request_type == "follow_up"
              else await proposal_gen.generate(db, job))
    if not result.get("used_llm"):
        raise HTTPException(503, "AI rewriting is unavailable. Your existing draft has been preserved; you can edit it manually.")
    db.refresh(item)
    if item.status != "pending_review" or item.revision != body.expected_revision:
        raise HTTPException(409, "The proposal changed during generation. Reload before using a new draft.")
    return {"text": result["humanized_text"], "revision": body.expected_revision,
            "warning": result.get("leak_warning")}


def _with_job(item: ProposalQueueItem, job: Job | None) -> ProposalQueueOut:
    out = ProposalQueueOut.model_validate(item)
    # "" (column default) serializes as null so the frontend can fall back
    # to proposal_text with a plain falsy check
    out.humanized_text = item.humanized_text or None
    out.job = JobOut.model_validate(job) if job else None
    return out


def _dispatch_fiverr_offer(db: Session, item: ProposalQueueItem, user: User) -> None:
    """Hand an APPROVED fiverr buyer_request offer to the stealth worker
    (submit_fiverr_offer) — the same queued_for_browser contract as the upwork
    path; gigs.py _apply_submission_outcome flips the item when the worker
    posts its verdict.

    Skips (item stays approved) when the tenant has no active fiverr account
    (the task would be doomed) or the circuit is open (mirrors the upwork
    guard: the skipped_circuit_open row stays visible in the UI)."""
    from ..approval import require_snapshot
    approved = require_snapshot(db, item)
    account = (db.query(PlatformAccount)
               .filter(PlatformAccount.user_id == user.id,
                       PlatformAccount.platform == "fiverr",
                       PlatformAccount.id == approved.get("account_id"),
                       PlatformAccount.enabled.is_(True),
                       PlatformAccount.mode != "disabled")
               .first())
    if account is None:
        item.submission_result = {
            **(item.submission_result or {}),
            "dispatch_note": ("no fiverr account enrolled — add one on the "
                              "Accounts page and the offer can be dispatched"),
        }
        db.add(AuditLog(user_id=user.id, action_type="buyer_request_dispatch_skipped",
                        platform="fiverr", detail={
                            "proposal_id": item.id, "reason": "no_fiverr_account",
                        }))
        db.commit()
        return
    from ..approval import require_snapshot
    require_snapshot(db, item)
    claimed = db.execute(update(ProposalQueueItem).where(
        ProposalQueueItem.id == item.id, ProposalQueueItem.user_id == user.id,
        ProposalQueueItem.status == "approved",
    ).values(status="submitting")).rowcount
    if not claimed:
        db.rollback()
        return
    db.refresh(item)
    require_snapshot(db, item)
    job = db.get(Job, item.job_id)
    task = enqueue_stealth_task(db, user.id, "fiverr", SUBMIT_FIVERR_OFFER, {
        "job_external_id": job.external_id if job else "",
        "job_url": (job.url if job else "") or None,
        "proposal_text": item.proposal_text,
        "humanized_text": item.proposal_text,
        "typing_plan": item.typing_plan or [],
        "bid_amount": item.bid_amount,
        "proposal_queue_item_id": item.id,
        "account_id": approved.get("account_id"),
    }, commit=False)
    if task is None or task.status == "skipped_circuit_open":
        item.status = "approved"
        db.commit()
        return  # leave approved — the item can be dispatched later
    item.status = "queued_for_browser"
    db.commit()


@router.get("", response_model=dict)
def list_proposals(status: str | None = Query(None),
                   request_type: str | None = Query(None),
                   limit: int = Query(50, ge=1, le=200),
                   offset: int = Query(0, ge=0),
                   db: Session = Depends(get_db),
                   user: User = Depends(get_current_user),
                   job_id: int | None = Query(None, ge=1)):
    q = scoped(db, ProposalQueueItem, user)
    if job_id is not None:
        q = q.filter(ProposalQueueItem.job_id == job_id)
    if status:
        q = q.filter(ProposalQueueItem.status == status)
    if request_type:
        q = q.filter(ProposalQueueItem.request_type == request_type)
    total = q.count()
    items = (q.order_by(ProposalQueueItem.created_at.desc())
             .offset(offset).limit(limit).all())
    # batch-load the page's jobs in one query (no per-item db.get)
    jobs = {}
    if items:
        jobs = {j.id: j for j in scoped(db, Job, user)
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
    return _with_job(item, get_owned(db, Job, item.job_id, user))


def _locked_proposal(db: Session, item_id: int, user: User) -> ProposalQueueItem | None:
    """Serialize review edits on PostgreSQL until their audit record commits."""
    return (db.query(ProposalQueueItem)
            .filter(ProposalQueueItem.id == item_id, ProposalQueueItem.user_id == user.id)
            .populate_existing().with_for_update().first())


@router.post("/{item_id}/approve", response_model=ProposalQueueOut)
def approve_proposal(item_id: int, body: ProposalReviewAction, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    item = _locked_proposal(db, item_id, user)
    if not item:
        raise HTTPException(404, "proposal not found")
    if item.revision != body.expected_revision:
        raise HTTPException(409, "proposal changed in another session; reload and review the latest version")
    if item.status != "pending_review":
        raise HTTPException(409, f"cannot approve a proposal in status '{item.status}'")
    if not body.reviewer:
        raise HTTPException(400, "reviewer identity is required")
    if body.proposal_text is not None and not body.proposal_text.strip():
        raise HTTPException(422, "proposal_text must not be empty")
    from ..approval import select_review_account
    select_review_account(db, item, body.platform_account_id)
    edited = False
    if body.proposal_text is not None and body.proposal_text != item.proposal_text:
        # version the PREVIOUS text before overwriting — the pre-edit draft
        # the reviewer actually saw must stay revertable (v1 = the original)
        versions = list(item.versions or [])
        versions.append({"text": item.proposal_text, "bid": item.bid_amount,
                         "by": body.reviewer, "at": datetime.now(timezone.utc).isoformat()})
        item.versions = versions
        item.proposal_text = body.proposal_text
        item.humanized_text = ""
        item.typing_plan = []
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
    item.status = "approved"
    item.reviewed_by = f"user:{user.id}"
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
    if item.platform == "fiverr" and item.request_type == "buyer_request":
        # approved buyer-request offers dispatch straight to the stealth
        # worker (there is no manual submit step for them)
        _dispatch_fiverr_offer(db, item, user)
        db.refresh(item)
    return _with_job(item, db.get(Job, item.job_id))


@router.post("/{item_id}/reject", response_model=ProposalQueueOut)
def reject_proposal(item_id: int, body: ProposalRejectAction, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    item = _locked_proposal(db, item_id, user)
    if not item:
        raise HTTPException(404, "proposal not found")
    if item.status != "pending_review":
        raise HTTPException(409, f"cannot reject a proposal in status '{item.status}'")
    item.status = "rejected"
    item.rejection_reason = body.reason
    item.rejection_notes = body.notes
    item.reviewed_by = f"user:{user.id}"
    item.reviewed_at = datetime.now(timezone.utc)
    from ..templates import record_rejection
    record_rejection(db, item, body.reason, body.notes, commit=False)
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
    if item.status != "submitted":
        raise HTTPException(409, "only confirmed submitted proposals can have outcomes")
    record_outcome(db, item, body.outcome)
    db.refresh(item)
    return _with_job(item, db.get(Job, item.job_id))


@router.post("/{item_id}/reconcile", response_model=ProposalQueueOut)
def reconcile_submission(item_id: int, body: SubmissionReconcileIn,
                         db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Resolve uncertain/failed work only after a human checks the platform."""
    item = get_owned(db, ProposalQueueItem, item_id, user)
    if not item:
        raise HTTPException(404, "proposal not found")
    if not body.evidence.strip() or len(body.evidence.strip()) < 10:
        raise HTTPException(422, "describe what you checked on the platform")
    previous = item.status
    now = datetime.now(timezone.utc)
    result = {**(item.submission_result or {}), "reconciliation": {
        "submitted": body.submitted, "evidence": body.evidence.strip(),
        "user_id": user.id, "at": now.isoformat()}}
    values = {"status": "submitted" if body.submitted else "pending_review",
              "submission_result": result}
    if body.submitted:
        values["submitted_at"] = now
    if not body.submitted:
        values.update(reviewed_by=None, reviewed_at=None)
    try:
        changed = db.execute(update(ProposalQueueItem).where(
            ProposalQueueItem.id == item.id, ProposalQueueItem.user_id == user.id,
            ProposalQueueItem.status.in_(["failed", "submitted_unverified"]),
            ProposalQueueItem.status == previous,
        ).values(**values)).rowcount
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "another active proposal already exists for this job; resolve it before returning this proposal to review")
    if not changed:
        db.rollback()
        raise HTTPException(409, "proposal no longer requires reconciliation")
    db.add(AuditLog(user_id=user.id, action_type="submission_reconciled", platform=item.platform,
                    detail={"proposal_id": item.id, "previous_status": previous, **result["reconciliation"]}))
    db.commit()
    db.refresh(item)
    return _with_job(item, db.get(Job, item.job_id))


@router.post("/{item_id}/mark-submitted", response_model=ProposalQueueOut)
def mark_submitted(item_id: int, body: MarkSubmittedAction | None = None,
                   db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Record that the user submitted this proposal BY HAND on the platform.

    For platforms with no automated submission channel: the user copies the
    approved text, submits it themselves, and marks it here so outcome
    tracking (hired/rejected/ghosted) works like any other proposal."""
    item = _locked_proposal(db, item_id, user)
    if not item:
        raise HTTPException(404, "proposal not found")
    if item.status not in ("approved", "failed"):
        raise HTTPException(
            409, f"cannot mark a proposal in status '{item.status}' as submitted")
    channel = (body.channel if body else None) or "manual"
    item.status = "submitted"
    item.submission_result = {**(item.submission_result or {}),
                              "channel": channel, "manual": True}
    db.add(AuditLog(user_id=user.id, action_type="proposal_marked_submitted",
                    platform=item.platform, detail={
                        "proposal_id": item.id, "job_id": item.job_id,
                        "channel": channel,
                    }))
    db.commit()
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
    if item.status != "submitted":
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
    check_llm_gen_rate(user)
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
    check_llm_gen_rate(user)  # cached responses above stay free
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
    for pid in sorted(set(body.ids)):
        item = _locked_proposal(db, pid, user)
        if not item or item.status != "pending_review" or item.needs_review:
            skipped.append(pid)
            continue
        if body.expected_revisions.get(pid) != item.revision:
            db.rollback()
            raise HTTPException(409, "selected proposal changed; reload and review the latest versions")
        from ..approval import select_review_account
        select_review_account(db, item)
        item.status = "approved"
        item.reviewed_by = f"user:{user.id}"
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
    # approved fiverr buyer-request offers dispatch to the stealth worker,
    # same as the single-approve path
    for pid in approved:
        item = db.get(ProposalQueueItem, pid)
        if (item is not None and item.platform == "fiverr"
                and item.request_type == "buyer_request"):
            _dispatch_fiverr_offer(db, item, user)
    return {"approved": approved, "skipped": skipped}


@router.post("/{item_id}/revert", response_model=ProposalQueueOut)
def revert_version(item_id: int, body: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Revert to a previous version (by index into `versions`)."""
    item = _locked_proposal(db, item_id, user)
    if not item:
        raise HTTPException(404, "proposal not found")
    if item.status not in ("pending_review", "approved"):
        raise HTTPException(409, f"cannot revert a proposal in status '{item.status}'")
    versions = item.versions or []
    idx = body.get("version_index", 0)
    if type(idx) is not int:
        raise HTTPException(422, "version_index must be an integer")
    if not (0 <= idx < len(versions)):
        raise HTTPException(400, f"version_index out of range (0-{len(versions)-1})")
    v = versions[idx]
    changed = False
    new_text = v.get("text", item.proposal_text)
    if new_text != item.proposal_text:
        item.proposal_text = new_text
        item.humanized_text = ""
        item.typing_plan = []
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


@router.post("/{item_id}/retry-generation", response_model=ProposalQueueOut)
def retry_generation(item_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Re-enqueue generation for a generation_failed item.

    Resets the auto-retry budget (submission_result.generation_retries) and
    enqueues the job's generation task, whose core regenerates THIS row in
    place (regenerate_failed_item) — generation_gates_pass would block a
    fresh item for the same job forever.
    """
    item = get_owned(db, ProposalQueueItem, item_id, user)
    if not item:
        raise HTTPException(404, "proposal not found")
    if item.status != "generation_failed":
        raise HTTPException(
            409, f"only generation_failed proposals can be retried (status is '{item.status}')")
    result = dict(item.submission_result or {})
    result["generation_retries"] = 0
    item.submission_result = result
    db.add(AuditLog(user_id=user.id, action_type="generation_retried", platform=item.platform, detail={
        "proposal_id": item.id, "job_id": item.job_id,
    }))
    db.commit()
    from ..work_queue import reset_generation
    job = db.get(Job, item.job_id)
    if job is None or not reset_generation(db, job):
        raise HTTPException(409, "generation is already running or the job is missing")
    from ..tasks import generate_proposal_task
    try:
        generate_proposal_task.delay(item.job_id)
    except Exception:  # noqa: BLE001 — broker down
        # the counter reset is committed, so the generation-retry beat will
        # pick the item up within its window anyway
        log.warning("retry-generation enqueue failed for item %d (broker down)", item.id)
        raise HTTPException(
            503, "task broker unavailable — the auto-retry tick will re-generate this item")
    db.refresh(item)
    return _with_job(item, db.get(Job, item.job_id))


@router.get("/templates/suggest", response_model=list[TemplateOut])
def suggest_templates(platform: str, skills: str = "", db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from ..templates import top_templates
    skill_list = [s.strip() for s in skills.split(",") if s.strip()]
    return top_templates(db, user.id, platform, skill_list)


@router.post("/templates/generate", response_model=dict)
async def generate_proposal_template(body: TemplateGenerateIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    body = body.model_dump()
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
    check_llm_gen_rate(user)
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
        text = ("Quick note on \"{job_title}\".\n\n"
                "Relevant evidence for review: {portfolio_piece}.\n\n"
                "Before agreeing an estimate, what is the must-have deliverable and acceptance criterion for the first milestone?")
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
    """Dispatch an APPROVED proposal through the platform's configured channel.

    freelancer.com → official bid API. upwork → agency-manager queue
    (browser handoff). Other platforms have no automated channel — submit by
    hand and use the mark-submitted endpoint.
    """
    item = get_owned(db, ProposalQueueItem, item_id, user)
    if not item:
        raise HTTPException(404, "proposal not found")
    if item.request_type == "follow_up":
        raise HTTPException(409, "follow-ups must be sent in the existing platform conversation")
    if not platform_enabled(db, user.id, item.platform):
        raise HTTPException(
            409, f"platform '{item.platform}' is disabled — enable it on the Accounts page")
    job = db.get(Job, item.job_id)
    if not job:
        raise HTTPException(404, "job not found")
    deadline = job.apply_deadline
    if deadline and deadline.replace(tzinfo=deadline.tzinfo or timezone.utc) <= datetime.now(timezone.utc):
        raise HTTPException(409, "application deadline has passed")
    if job.status == "archived":
        raise HTTPException(409, "archived jobs cannot be submitted")
    from ..approval import require_snapshot
    approved = require_snapshot(db, item)
    if item.platform in ("freelancer", "upwork") and not approved.get("account_id"):
        raise HTTPException(400, "enroll an enabled account on the Accounts page and review this proposal again")
    # atomic claim approved → submitting BEFORE any external call, so a
    # concurrent/duplicate submit (double-click, client retry) loses the race
    # here instead of placing a second real-money bid — same conditional-update
    # pattern as the stealth-task claim in gigs.py
    res = db.execute(
        update(ProposalQueueItem)
        .where(ProposalQueueItem.id == item.id,
               ProposalQueueItem.status == "approved")
        .values(status="submitting")
    )
    db.commit()
    if res.rowcount == 0:
        raise HTTPException(409, "proposal is not approved (already submitted or in flight)")

    # The approved row may have changed while this request waited for its claim.
    db.refresh(item)
    try:
        require_snapshot(db, item)
    except HTTPException:
        item.status = "pending_review"
        item.reviewed_by = None
        item.reviewed_at = None
        db.commit()
        raise
    external_write_attempted = False
    channel = ""
    response_id = None
    try:
        if item.platform == "freelancer":
            from ..adapters.freelancer import FreelancerAdapter

            bidder_id = int(approved.get("bidder_id") or 0)
            if not bidder_id:
                raise HTTPException(
                    400, "no Freelancer bidder id: set 'bidder_id' in the freelancer "
                    "account's settings on the Accounts page"
                )
            account = db.get(PlatformAccount, approved.get("account_id")) if approved.get("account_id") else None
            adapter = FreelancerAdapter(db, user.id, principal=account.principal if account else "default")
            try:
                external_write_attempted = True
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

            on_behalf_of = approved.get("agency_member") or ""
            if not on_behalf_of:
                raise HTTPException(
                    400, "no Upwork agency member: set 'on_behalf_of' in the upwork "
                    "account's settings on the Accounts page"
                )
            connects_required = item.submission_result.get("connects_required", 0)
            account = db.get(PlatformAccount, approved.get("account_id")) if approved.get("account_id") else None
            adapter = UpworkAgencyAdapter(db, user.id, principal=account.principal if account else "agency_manager")
            try:
                record = adapter.submit_proposal(
                    job_external_id=job.external_id,
                    proposal_text=item.proposal_text,
                    on_behalf_of=on_behalf_of,
                    connects_required=connects_required,
                    approved_by=item.reviewed_by,
                    persist=False,
                )
            finally:
                await adapter.close()
            channel = "upwork_agency_queue"
            response_id = record.get("id")
            item.submission_result = {"channel": channel, "record": record, "on_behalf_of": on_behalf_of, "connects_required": connects_required}
            # handoff to the stealth-browser worker (AD-4): it executes the
            # agency BM submission and completes this task, which flips the
            # item out of queued_for_browser.
            stealth_task = enqueue_stealth_task(db, user.id, "upwork", SUBMIT_UPWORK_PROPOSAL, {
                "job_external_id": job.external_id,
                "job_url": job.url,
                "proposal_text": item.proposal_text,
                "humanized_text": item.proposal_text,
                "typing_plan": item.typing_plan or [],
                "on_behalf_of": on_behalf_of,
                "agency_id": (account.settings or {}).get("agency_id", "") if account else "",
                "connects_required": connects_required,
                "bid_amount": item.bid_amount,
                "proposal_queue_item_id": item.id,
                "account_id": account.id,
            }, commit=False)
            if stealth_task is None or stealth_task.status == "skipped_circuit_open":
                # circuit open: the task will never run — leave the item
                # approved instead of stranding it in queued_for_browser
                reason = ((stealth_task.result or {}).get("reason", "")
                          if stealth_task is not None else "")
                if not reason:
                    from .. import circuit_breaker
                    reason = circuit_breaker.check("upwork", user.id, db=db)[1] or "upwork circuit is open"
                raise HTTPException(409, reason)
        else:
            raise HTTPException(
                400,
                f"submission for '{item.platform}' isn't automated — submit on "
                "the platform and use 'Mark as submitted'",
            )
    except HTTPException:
        # pre-dispatch failure (missing bidder config, unsupported platform,
        # circuit open): release the claim so the item can be fixed and
        # submitted again instead of stranding in "submitting"
        item.status = "approved"
        db.commit()
        raise
    except Exception:  # noqa: BLE001
        item.status = "submitted_unverified" if external_write_attempted else "failed"
        item.submission_result = {"error": "Submission outcome could not be confirmed. Check the platform before retrying." if external_write_attempted else "Submission failed before dispatch."}
        db.commit()
        # adapter/library exception strings leak upstream internals — the
        # detail stays in the server log
        log.exception("proposal submission failed for item %d", item.id)
        raise HTTPException(502, "submission failed")

    # Upwork submissions wait for the external browser worker to confirm;
    # only Freelancer bids are truly "submitted" at this point.
    item.status = "submitted" if item.platform == "freelancer" else "queued_for_browser"
    db.add(AuditLog(user_id=user.id, action_type="proposal_submitted" if item.platform == "freelancer" else "proposal_queued", platform=item.platform, detail={
        "proposal_id": item.id, "job_id": job.id, "channel": channel,
        "platform_response_id": response_id, "approved_by": item.reviewed_by,
    }))
    db.commit()
    db.refresh(item)
    return _with_job(item, db.get(Job, item.job_id))


@router.post("/{item_id}/return-to-review", response_model=ProposalQueueOut)
def return_to_review(item_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    item = _locked_proposal(db,item_id,user)
    if item is None:
        raise HTTPException(404,"proposal not found")
    if item.status != "approved":
        raise HTTPException(409,"only an unsent approved proposal can return directly to review")
    item.status="pending_review"
    item.reviewed_at=None
    item.reviewed_by=None
    db.add(AuditLog(user_id=user.id,action_type="approval_invalidated",platform=item.platform,detail={"proposal_id":item.id}))
    db.commit();db.refresh(item)
    return _with_job(item,db.get(Job,item.job_id))
