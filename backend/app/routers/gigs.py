"""Gig management endpoints: templates, creation triggers, analytics,
competitor intel, buyer-request inbox, and stealth-task handoff."""
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import get_args

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import update
from sqlalchemy.orm import Session

from .. import circuit_breaker, fiverr_monitor, gig_templates as gt
from ..auth import (get_current_user, get_owned, get_worker,
                    get_worker_or_user, scoped)
from ..database import get_db
from ..gig_analytics import (enqueue_metrics_scrape, record_metrics,
                             store_competitor_snapshot)
from ..models import (AuditLog, Gig, GigMetric, GigTemplate, CompetitorSnapshot,
                      PlatformAccount, ProposalQueueItem, StealthTask, User)
from ..ratelimit import check_llm_gen_rate
from ..schemas import (CompetitorSnapshotOut, GigMetricIn, GigMetricOut,
                       GigOut, GigTemplateIn, GigTemplateOut,
                       Platform, StealthTaskClaimIn)
from ..stealth import SUBMIT_UPWORK_PROPOSAL

router = APIRouter(prefix="/api/gigs", tags=["gigs"])

log = logging.getLogger(__name__)

# circuit breaker trips after this many stealth failures within the window
STEALTH_FAILURE_WINDOW = timedelta(hours=1)
STEALTH_FAILURE_THRESHOLD = 3


# --- taxonomy & SEO helpers ---

@router.get("/taxonomy/fiverr", response_model=dict)
def fiverr_taxonomy(user: User = Depends(get_current_user)):
    return {"categories": gt.FIVERR_CATEGORIES,
            "note": "seed dataset — refresh from Fiverr seller dashboard when it drifts"}


@router.post("/seo-title-score", response_model=dict)
def seo_title_score(body: dict, user: User = Depends(get_current_user)):
    return gt.seo_title_score(body.get("title", ""), body.get("keywords") or [])


@router.post("/faqs/generate", response_model=dict)
async def generate_faqs(body: dict, user: User = Depends(get_current_user)):
    check_llm_gen_rate(user)
    faqs = await gt.generate_faqs(body.get("gig_type", ""), body.get("title", ""),
                                  int(body.get("count", 4)))
    return {"faqs": faqs}


# --- template CRUD ---

@router.get("/templates", response_model=list[GigTemplateOut])
def list_templates(platform: str | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    q = scoped(db, GigTemplate, user)
    if platform:
        q = q.filter(GigTemplate.platform == platform)
    return q.all()


@router.post("/templates", response_model=GigTemplateOut, status_code=201)
def create_template(body: GigTemplateIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    tpl, problems = gt.create_template(db, user.id, body.platform, body.name,
                                       body.template_json, body.auto_publish)
    if problems:
        raise HTTPException(422, {"validation": problems})
    return tpl


@router.put("/templates/{tpl_id}", response_model=GigTemplateOut)
def update_template(tpl_id: int, body: GigTemplateIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    tpl = get_owned(db, GigTemplate, tpl_id, user)
    if not tpl:
        raise HTTPException(404, "gig template not found")
    validator = (gt.validate_fiverr_template if body.platform == "fiverr"
                 else gt.validate_upwork_catalog_template if body.platform == "upwork"
                 else lambda d: [])
    problems = validator(body.template_json)
    if problems:
        raise HTTPException(422, {"validation": problems})
    tpl.platform, tpl.name, tpl.template_json = body.platform, body.name, body.template_json
    tpl.auto_publish = body.auto_publish
    db.commit()
    db.refresh(tpl)
    return tpl


@router.delete("/templates/{tpl_id}", status_code=204)
def delete_template(tpl_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    tpl = get_owned(db, GigTemplate, tpl_id, user)
    if not tpl:
        raise HTTPException(404, "gig template not found")
    db.delete(tpl)
    db.commit()


@router.post("/templates/{tpl_id}/toggle", response_model=GigTemplateOut)
def toggle_template(tpl_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    tpl = get_owned(db, GigTemplate, tpl_id, user)
    if not tpl:
        raise HTTPException(404, "gig template not found")
    tpl.is_active = not tpl.is_active
    db.commit()
    db.refresh(tpl)
    return tpl


# --- gig creation (queues stealth task; DRAFT only for Fiverr) ---

@router.post("/templates/{tpl_id}/create-gig", response_model=dict)
def create_gig_from_template(tpl_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    tpl = get_owned(db, GigTemplate, tpl_id, user)
    if not tpl:
        raise HTTPException(404, "gig template not found")
    if tpl.platform == "fiverr":
        task, err = fiverr_monitor.queue_gig_creation(db, tpl)
    elif tpl.platform == "upwork":
        task, err = fiverr_monitor.queue_upwork_catalog_upsert(db, tpl)
    else:
        raise HTTPException(400, f"gig creation not supported for '{tpl.platform}'")
    if err:
        raise HTTPException(429, err)
    return {"stealth_task_id": task.id, "status": task.status,
            "note": "gig will be saved as DRAFT — never auto-published"
                    if tpl.platform == "fiverr" else
                    f"auto_publish={'on' if tpl.auto_publish else 'off (draft for review)'}"}


# --- gigs & metrics ---

@router.get("", response_model=list[GigOut])
def list_gigs(platform: str | None = None, status: str | None = None,
              db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    q = scoped(db, Gig, user)
    if platform:
        q = q.filter(Gig.platform == platform)
    if status:
        q = q.filter(Gig.status == status)
    return q.all()


@router.post("", response_model=GigOut, status_code=201)
def register_gig(body: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Register an externally-created gig for tracking."""
    platform = body.get("platform")
    if platform not in get_args(Platform):
        raise HTTPException(422, f"unsupported platform {platform!r} — "
                                 f"must be one of {list(get_args(Platform))}")
    template_id = body.get("template_id")
    if template_id is not None and not get_owned(db, GigTemplate, template_id, user):
        raise HTTPException(404, "gig template not found")
    gig = Gig(
        user_id=user.id,
        platform=platform, title=body.get("title", ""),
        external_id=body.get("external_id", ""), url=body.get("url", ""),
        status=body.get("status", "draft"), price_min=body.get("price_min"),
        template_id=template_id,
    )
    db.add(gig)
    db.commit()
    db.refresh(gig)
    return gig


@router.get("/metrics", response_model=list[GigMetricOut])
def list_metrics(gig_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return (scoped(db, GigMetric, user).filter(GigMetric.gig_id == gig_id)
            .order_by(GigMetric.week).all())


@router.post("/metrics", response_model=GigMetricOut, status_code=201)
def ingest_metrics(body: GigMetricIn, db: Session = Depends(get_db),
                   principal: User | None = Depends(get_worker_or_user)):
    """Stealth worker posts weekly scrape results here (worker token), or the
    owning user via the UI. Tenancy resolves from the gig, not the token."""
    if principal is None:  # worker token: resolve the gig cross-tenant
        gig = db.get(Gig, body.gig_id)
    else:
        gig = get_owned(db, Gig, body.gig_id, principal)
    if not gig:
        raise HTTPException(404, "gig not found")
    return record_metrics(db, gig, body.impressions, body.clicks,
                          body.orders, body.revenue, body.week)


@router.post("/metrics/scrape", response_model=dict)
def trigger_metrics_scrape(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    tasks = enqueue_metrics_scrape(db, user.id)
    return {"queued_tasks": [t.id for t in tasks]}


# --- competitor intel ---

@router.get("/competitors", response_model=list[CompetitorSnapshotOut])
def list_competitor_snapshots(platform: str, category: str | None = None,
                              db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    q = scoped(db, CompetitorSnapshot, user).filter(CompetitorSnapshot.platform == platform)
    if category:
        q = q.filter(CompetitorSnapshot.category == category)
    return q.order_by(CompetitorSnapshot.created_at.desc()).limit(20).all()


@router.post("/competitors", response_model=CompetitorSnapshotOut, status_code=201)
def ingest_competitor_snapshot(body: dict, db: Session = Depends(get_db),
                               principal: User | None = Depends(get_worker_or_user)):
    """Stealth worker posts top-10 category scrape results here. The worker
    token is cross-tenant, so worker posts must carry `user_id` (from the
    stealth task payload)."""
    if principal is None:
        user_id = body.get("user_id")
        if not user_id:
            raise HTTPException(422, "user_id required for worker posts")
    else:
        user_id = principal.id
    return store_competitor_snapshot(db, user_id, body["platform"], body["category"],
                                     body.get("gigs", []), body.get("my_price"))


# --- buyer request inbox ---

@router.get("/buyer-requests", response_model=dict)
def buyer_request_inbox(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from ..models import ProposalQueueItem
    items = (scoped(db, ProposalQueueItem, user)
             .filter(ProposalQueueItem.request_type == "buyer_request")
             .order_by(ProposalQueueItem.created_at.desc()).limit(100).all())
    return {"offers_remaining_today": fiverr_monitor.offers_remaining_today(user.id),
            "daily_limit": fiverr_monitor.FIVERR_DAILY_OFFER_LIMIT,
            "count": len(items)}


@router.post("/buyer-requests/process", response_model=dict)
def process_buyer_requests(body: dict, db: Session = Depends(get_db),
                           principal: User | None = Depends(get_worker_or_user)):
    """Stealth worker posts scraped buyer requests here for filtering + offers.
    Worker posts must carry `user_id` (from the stealth task payload)."""
    if principal is None:
        user_id = body.get("user_id")
        if not user_id:
            raise HTTPException(422, "user_id required for worker posts")
    else:
        user_id = principal.id
    return fiverr_monitor.process_buyer_requests(db, user_id, body.get("requests", []))


# --- stealth task handoff (browser worker polling) ---

@router.get("/stealth-session", response_model=dict)
def get_stealth_session(platform: str, user_id: int, db: Session = Depends(get_db),
                        worker: str = Depends(get_worker)):
    """Worker-token-only: the enrolled browser session for (platform, user_id).

    Lets the worker seed its browser context from the vault (credentials
    enrolled via the Accounts UI) instead of the CLI login flow. Tenancy
    comes from the explicit user_id — the worker pool serves all tenants,
    same as stealth-task polling. Secret values other than the storage_state
    itself are never returned. Also carries the account's `proxy_url`
    setting (None when unset) so each tenant's traffic can be isolated to
    its own exit IP instead of sharing one platform-level proxy, plus
    optional `timezone`/`locale` settings so the worker can align the
    browser fingerprint's geo with the account/proxy geo.

    The read is scoped to an active claim: the worker only fetches a session
    while executing a claimed task for this (platform, user_id), so without
    one the session stays sealed. Every read is audit-logged.
    """
    from ..adapters.vault import CredentialVault
    claimed = (db.query(StealthTask)
               .filter(StealthTask.user_id == user_id,
                       StealthTask.platform == platform,
                       StealthTask.status == "claimed")
               .first())
    if claimed is None:
        raise HTTPException(409, "no claimed stealth task for this platform/user — "
                                 "sessions are only served while a task is executing")
    db.add(AuditLog(user_id=user_id, action_type="stealth_session_read",
                    platform=platform,
                    detail={"task_id": claimed.id, "worker": worker}))
    db.commit()
    account = (db.query(PlatformAccount)
               .filter(PlatformAccount.user_id == user_id,
                       PlatformAccount.platform == platform,
                       PlatformAccount.enabled.is_(True),
                       PlatformAccount.mode != "disabled",
                       PlatformAccount.credential_ref != "")
               .order_by(PlatformAccount.created_at)
               .first())
    if not account:
        disabled = (db.query(PlatformAccount)
                    .filter(PlatformAccount.user_id == user_id,
                            PlatformAccount.platform == platform,
                            PlatformAccount.mode == "disabled")
                    .first())
        if disabled:
            raise HTTPException(
                409, f"platform '{platform}' is disabled — enable it on the Accounts page")
        return {"storage_state": None, "credentials_present": False, "proxy_url": None,
                "timezone": None, "locale": None}
    settings = account.settings or {}
    geo = {"proxy_url": settings.get("proxy_url"),
           "timezone": settings.get("timezone"),
           "locale": settings.get("locale")}
    creds = CredentialVault(db, user_id).load(platform, account.principal)
    if not creds:
        return {"storage_state": None, "credentials_present": False, **geo}
    storage_state = None
    raw_state = creds.get("storage_state_json")
    if raw_state:
        try:
            storage_state = json.loads(raw_state)
        except (ValueError, TypeError):
            log.warning("stealth-session %s/%s: stored storage_state_json is invalid",
                        platform, user_id)
    return {"storage_state": storage_state, "credentials_present": True, **geo}


def _task_out(t: StealthTask) -> dict:
    return {"id": t.id, "user_id": t.user_id, "platform": t.platform,
            "task_type": t.task_type, "payload": t.payload, "status": t.status,
            "claimed_by": t.claimed_by, "created_at": t.created_at}


@router.get("/stealth-tasks", response_model=list[dict])
def poll_stealth_tasks(platform: str | None = None, status: str = "pending",
                       db: Session = Depends(get_db),
                       principal: User | None = Depends(get_worker_or_user)):
    """Worker token → pending tasks across all tenants (the pool serves every
    user); user JWT → the caller's own tasks (UI display)."""
    q = db.query(StealthTask) if principal is None else scoped(db, StealthTask, principal)
    q = q.filter(StealthTask.status == status)
    if platform:
        q = q.filter(StealthTask.platform == platform)
    return [_task_out(t) for t in q.order_by(StealthTask.created_at).limit(50).all()]


@router.post("/stealth-tasks/{task_id}/claim", response_model=dict)
def claim_stealth_task(task_id: int, body: StealthTaskClaimIn,
                       db: Session = Depends(get_db),
                       worker: str = Depends(get_worker)):
    """Atomically claim a pending task so no two workers execute it."""
    now = datetime.now(timezone.utc)
    res = db.execute(
        update(StealthTask)
        .where(StealthTask.id == task_id, StealthTask.status == "pending")
        .values(status="claimed", claimed_by=body.worker_id, claimed_at=now)
    )
    db.commit()
    if res.rowcount == 0:
        task = db.get(StealthTask, task_id)
        if task is None:
            raise HTTPException(404, "stealth task not found")
        raise HTTPException(409, f"stealth task already {task.status}")
    return _task_out(db.get(StealthTask, task_id))


@router.post("/stealth-tasks/{task_id}/complete", response_model=dict)
def complete_stealth_task(task_id: int, body: dict, db: Session = Depends(get_db),
                          worker: str = Depends(get_worker)):
    task = db.get(StealthTask, task_id)
    if not task:
        raise HTTPException(404, "stealth task not found")
    # only the claiming worker may complete its own task
    if task.status != "claimed":
        raise HTTPException(409, f"stealth task already {task.status}")
    if task.claimed_by != body.get("worker_id"):
        raise HTTPException(409, "stealth task claimed by another worker")
    success = body.get("success", True)
    now = datetime.now(timezone.utc)
    task.status = "done" if success else "failed"
    task.result = body.get("result", {})
    task.completed_at = now
    if not success:
        # windowed failure counting: trip after N failures in the last hour.
        # Scoped to the tenant — one user's failing session must not halt
        # every other tenant's enqueues on the platform.
        db.flush()
        recent_failures = (db.query(StealthTask)
                           .filter(StealthTask.platform == task.platform,
                                   StealthTask.user_id == task.user_id,
                                   StealthTask.status == "failed",
                                   StealthTask.completed_at >= now - STEALTH_FAILURE_WINDOW)
                           .count())
        if recent_failures >= STEALTH_FAILURE_THRESHOLD:
            circuit_breaker.open_circuit(
                task.platform,
                f"{recent_failures} stealth task failures in the last hour",
                user_id=task.user_id)
    _apply_submission_outcome(db, task, success)
    if (task.result or {}).get("session_expired"):
        _flag_session_expired(db, task)
    db.add(AuditLog(user_id=task.user_id, action_type="stealth_task_completed",
                    platform=task.platform,
                    detail={"task_id": task.id, "worker_id": task.claimed_by,
                            "success": success}))
    db.commit()
    return {"id": task.id, "status": task.status}


def _flag_session_expired(db: Session, task: StealthTask):
    """The worker found a dead session (login redirect / logged-out page):
    audit it and flag the platform account as needing re-enrollment so a
    human re-enrolls credentials instead of the worker silently posting
    fabricated data. The failed task already counts toward the per-tenant
    circuit breaker via the normal failure path."""
    db.add(AuditLog(user_id=task.user_id, action_type="session_expired",
                    platform=task.platform,
                    detail={"task_id": task.id, "worker_id": task.claimed_by}))
    account = (db.query(PlatformAccount)
               .filter(PlatformAccount.user_id == task.user_id,
                       PlatformAccount.platform == task.platform)
               .order_by(PlatformAccount.created_at)
               .first())
    if account is not None:
        account.settings = {**(account.settings or {}),
                            "needs_reenrollment": True}


def _apply_submission_outcome(db: Session, task: StealthTask, success: bool):
    """Close the HITL loop for submission tasks: flip the review-queue item
    out of queued_for_browser and complete the agency handoff record.

    The worker's explicit `result.submitted` verdict wins over the bare
    task-level success flag: a task can "succeed" (no crash) while the
    platform rejected the submit, or while the outcome could not be
    confirmed — the click already happened, so the latter becomes
    submitted_unverified for a human to check on the platform (NEVER
    auto-retried: a blind retry risks a duplicate proposal)."""
    item_id = (task.payload or {}).get("proposal_queue_item_id")
    if not item_id:
        return
    result = task.result or {}
    submitted = result.get("submitted")
    item = db.get(ProposalQueueItem, item_id)
    if item and item.user_id == task.user_id and item.status == "queued_for_browser":
        if not success:
            item.status = "failed"
            item.submission_result = {
                **(item.submission_result or {}),
                "error": result.get("error", "stealth submission failed"),
            }
        elif submitted is False:
            # explicit non-submission on a successful task: the platform
            # confirmed a rejection, or the manual-assist gate left the final
            # click to a human. Map to failed with the worker's reason so a
            # human re-reviews — leaving it in queued_for_browser would
            # strand it, since nothing enqueues a new task for an item
            # already in that state.
            item.status = "failed"
            item.submission_result = {
                **(item.submission_result or {}),
                "error": (result.get("reason") or result.get("note")
                          or "submission not confirmed by the platform"),
            }
        elif submitted is None and result.get("state") == "submitted_unverified":
            item.status = "submitted_unverified"
            item.submission_result = {**(item.submission_result or {}), **result}
        else:
            item.status = "submitted"
    if task.task_type == SUBMIT_UPWORK_PROPOSAL:
        from ..adapters.upwork_agency import UpworkAgencyAdapter
        # the agency handoff record is only closed on a CONFIRMED outcome —
        # an unverified submit stays pending for human reconciliation
        unverified = (success and submitted is None
                      and result.get("state") == "submitted_unverified")
        if not unverified:
            UpworkAgencyAdapter(db, task.user_id).complete_submission(
                (task.payload or {}).get("job_external_id", ""),
                success and submitted is not False,
                note="stealth worker " +
                     ("submitted" if success and submitted is not False
                      else "failed"))


# --- worker-posted proposal status (Upwork outcome/reply sync) ---

@router.post("/proposal-status", response_model=dict)
async def ingest_proposal_status(body: dict, db: Session = Depends(get_db),
                                 worker: str = Depends(get_worker)):
    """Worker-token-only: results of a scrape_proposal_status task.

    Body: {task_id, results: [{proposal_queue_item_id, platform_status,
    has_unread_reply}]}. hired → outcome hired, declined → rejected, unread
    reply → client_replied_at + client_replied WS event. Idempotent, and the
    stealth task is completed on success (worker failures should use the
    regular /complete endpoint with success=false instead).
    """
    from ..proposal_status_sync import apply_proposal_status_results
    from ..stealth import SCRAPE_PROPOSAL_STATUS

    task_id = body.get("task_id")
    results = body.get("results")
    if not task_id or not isinstance(results, list):
        raise HTTPException(422, "task_id and results (list) are required")
    task = db.get(StealthTask, task_id)
    if task is None or task.task_type != SCRAPE_PROPOSAL_STATUS:
        raise HTTPException(404, "scrape_proposal_status task not found")
    # the worker posts while it holds the claim; "done" is accepted so an
    # idempotent repost after server-side completion stays a no-op
    if task.status not in ("claimed", "done"):
        raise HTTPException(409, f"scrape_proposal_status task is '{task.status}', "
                                 "not claimed")

    summary = await apply_proposal_status_results(db, task, results)
    db.add(AuditLog(user_id=task.user_id, action_type="proposal_status_ingested",
                    platform=task.platform,
                    detail={"task_id": task.id, "results_count": len(results),
                            **summary}))
    if task.status == "claimed":
        task.status = "done"
        task.result = {"results_count": len(results), **summary}
        task.completed_at = datetime.now(timezone.utc)
    db.commit()
    return {"task_id": task.id, "task_status": task.status, **summary}


# --- circuit breaker controls ---

@router.get("/circuit/{platform}", response_model=dict)
def circuit_state(platform: str, user: User = Depends(get_current_user)):
    return circuit_breaker.get_state(platform)


@router.post("/circuit/{platform}", response_model=dict)
def set_circuit(platform: str, body: dict, user: User = Depends(get_current_user)):
    state = body.get("state")
    if state == "open":
        circuit_breaker.open_circuit(platform, body.get("reason", "manual"))
    elif state == "closed":
        circuit_breaker.close_circuit(platform, body.get("reason", "manual reset"))
    else:
        raise HTTPException(400, "state must be 'open' or 'closed'")
    return circuit_breaker.get_state(platform)
