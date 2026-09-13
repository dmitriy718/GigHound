from ..pagination import PageLimit, PageOffset
from ..schemas import SeoTitleIn, FaqGenerateIn, GigRegisterIn, GigAccountIn
"""Gig management endpoints: templates, creation triggers, analytics,
competitor intel, buyer-request inbox, and stealth-task handoff."""
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import get_args

from fastapi import APIRouter, Depends, Header, HTTPException, Query
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
from ..ws_manager import alerts

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
def seo_title_score(body: SeoTitleIn, user: User = Depends(get_current_user)):
    body = body.model_dump()
    return gt.seo_title_score(body.get("title", ""), body.get("keywords") or [])


@router.post("/faqs/generate", response_model=dict)
async def generate_faqs(body: FaqGenerateIn, user: User = Depends(get_current_user)):
    body = body.model_dump()
    check_llm_gen_rate(user)
    faqs = await gt.generate_faqs(body.get("gig_type", ""), body.get("title", ""),
                                  int(body.get("count", 4)))
    return {"faqs": faqs}


# --- template CRUD ---

@router.get("/templates", response_model=list[GigTemplateOut])
def list_templates(platform: str | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user), limit: PageLimit = 100, offset: PageOffset = 0):
    q = scoped(db, GigTemplate, user)
    if platform:
        q = q.filter(GigTemplate.platform == platform)
    return q.order_by(GigTemplate.id).offset(offset).limit(limit).all()


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
def create_gig_from_template(tpl_id: int, account_id: int | None = Query(None, ge=1), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    tpl = get_owned(db, GigTemplate, tpl_id, user)
    if not tpl:
        raise HTTPException(404, "gig template not found")
    if tpl.platform == "fiverr":
        accounts = db.query(PlatformAccount).filter(
            PlatformAccount.user_id == user.id, PlatformAccount.platform == tpl.platform,
            PlatformAccount.enabled.is_(True), PlatformAccount.mode.in_(["stealth", "hybrid"]))
        if account_id is not None:
            accounts = accounts.filter(PlatformAccount.id == account_id)
        candidates = accounts.limit(2).all()
        if not candidates:
            raise HTTPException(409, "Connect an enabled Fiverr browser account before creating a draft")
        if len(candidates) != 1:
            raise HTTPException(409, "Choose the Fiverr account for this draft")
        task, err = fiverr_monitor.queue_gig_creation(db, tpl, account=candidates[0])
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
              db: Session = Depends(get_db), user: User = Depends(get_current_user), limit: PageLimit = 100, offset: PageOffset = 0):
    q = scoped(db, Gig, user)
    if platform:
        q = q.filter(Gig.platform == platform)
    if status:
        q = q.filter(Gig.status == status)
    return q.order_by(Gig.id).offset(offset).limit(limit).all()


def _gig_seller(db, user_id, platform, account_id):
    if account_id is None:
        return None
    account = db.query(PlatformAccount).filter_by(id=account_id, user_id=user_id, platform=platform).first()
    if account is None or not account.enabled or account.mode not in ("stealth", "hybrid"):
        raise HTTPException(409, "Select an enabled browser account for this platform")
    return account


@router.put("/{gig_id}/account", response_model=GigOut)
def assign_gig_account(gig_id: int, body: GigAccountIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    gig = get_owned(db, Gig, gig_id, user)
    if gig is None:
        raise HTTPException(404, "gig not found")
    db.refresh(gig, with_for_update=True)
    if gig.account_binding_version != body.expected_version:
        raise HTTPException(409, "Seller assignment changed; reload before saving")
    account = _gig_seller(db, user.id, gig.platform, body.account_id)
    gig.account_id = account.id if account else None
    gig.account_epoch = account.identity_epoch if account else None
    gig.account_binding_version += 1
    db.commit()
    db.refresh(gig)
    return gig


@router.post("", response_model=GigOut, status_code=201)
def register_gig(body: GigRegisterIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    body = body.model_dump()
    """Register an externally-created gig for tracking."""
    platform = body.get("platform")
    if platform not in get_args(Platform):
        raise HTTPException(422, f"unsupported platform {platform!r} — "
                                 f"must be one of {list(get_args(Platform))}")
    template_id = body.get("template_id")
    if template_id is not None:
        template = get_owned(db, GigTemplate, template_id, user)
        if template is None:
            raise HTTPException(404, "gig template not found")
        if template.platform != platform:
            raise HTTPException(422, "gig and template platforms must match")
    account = _gig_seller(db, user.id, platform, body.get("account_id"))
    gig = Gig(
        account_id=account.id if account else None,
        account_epoch=account.identity_epoch if account else None,
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
    from sqlalchemy import func
    latest = db.query(func.max(GigMetric.id)).filter(GigMetric.user_id==user.id,GigMetric.gig_id==gig_id).group_by(GigMetric.week)
    return db.query(GigMetric).filter(GigMetric.id.in_(latest)).order_by(GigMetric.week).all()


@router.post("/metrics", response_model=GigMetricOut, status_code=201)
def ingest_metrics(body: GigMetricIn, db: Session = Depends(get_db),
                   principal: User | None = Depends(get_worker_or_user)):
    """Stealth worker posts weekly scrape results here (worker token), or the
    owning user via the UI. Tenancy resolves from the gig, not the token."""
    if principal is None:
        task = _worker_result_task(db, body.model_dump(), "scrape_gig_metrics")
        listed = {g.get("id") for g in (task.payload or {}).get("gigs", []) if isinstance(g,dict)}
        if body.gig_id not in listed:
            raise HTTPException(409, "gig was not part of the claimed task")
        gig = db.query(Gig).filter_by(id=body.gig_id,user_id=task.user_id,platform=task.platform).one_or_none()
    else:
        gig = get_owned(db, Gig, body.gig_id, principal)
    if not gig:
        raise HTTPException(404, "gig not found")
    if principal is None:
        db.refresh(gig, with_for_update=True)
        source = next(g for g in task.payload['gigs'] if g.get('id') == gig.id)
        if (gig.account_id != task.payload.get('account_id') or
            gig.account_epoch != task.payload.get('account_epoch') or
            source.get('account_binding_version') != gig.account_binding_version):
            raise HTTPException(409, "Gig seller assignment changed; collect fresh metrics")
    return record_metrics(db, gig, body.impressions, body.clicks,
                          body.orders, body.revenue, body.week)


@router.post("/metrics/scrape", response_model=dict)
def trigger_metrics_scrape(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    report = {}
    tasks = enqueue_metrics_scrape(db, user.id, report=report)
    return {"queued_tasks": [t.id for t in tasks], **report}


# --- competitor intel ---
# RESERVED/UNWIRED (P5-1): the scrape_competitors worker handler and these
# endpoints work, but no backend producer enqueues the task — there is no
# user-facing competitor-tracking config to drive one, and we don't invent
# one here. Snapshots can still be posted (worker or manual) and listed.

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
    if not isinstance(body.get("category"),str) or not body["category"].strip() or body.get("platform") not in get_args(Platform):
        raise HTTPException(422, "platform and category are required")
    if not isinstance(body.get("gigs", []),list) or len(body.get("gigs", [])) > 100:
        raise HTTPException(422, "gigs must be a list of at most 100 records")
    import math
    for gig in body.get("gigs", []):
        if not isinstance(gig, dict):
            raise HTTPException(422, "each competitor gig must be an object")
        price = gig.get("price")
        if price is not None and (isinstance(price, bool) or not isinstance(price, (float,int)) or not math.isfinite(price) or price < 0):
            raise HTTPException(422, "competitor prices must be finite nonnegative numbers")
    mine = body.get("my_price")
    if mine is not None and (isinstance(mine, bool) or not isinstance(mine,(float,int)) or not math.isfinite(mine) or mine < 0):
        raise HTTPException(422, "my_price must be a finite nonnegative number")
    task = None
    if principal is None:
        task = _worker_result_task(db, body, "scrape_competitors")
        if task.payload.get("category") != body["category"]:
            raise HTTPException(422, "category does not match the claimed task")
        existing_id = (task.result or {}).get("competitor_snapshot_id")
        if existing_id:
            existing = db.get(CompetitorSnapshot, existing_id)
            if existing is not None:
                return existing
        user_id = body.get("user_id")
        if not user_id:
            raise HTTPException(422, "user_id required for worker posts")
    else:
        user_id = principal.id
    snap = store_competitor_snapshot(db, user_id, body["platform"], body["category"],
                                    body.get("gigs", []), body.get("my_price"), commit=False)
    if task is not None:
        task.result = {**(task.result or {}), "competitor_snapshot_id": snap.id}
    db.commit()
    return snap


# --- buyer request inbox ---

@router.get("/buyer-requests", response_model=dict)
def buyer_request_inbox(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from ..models import ProposalQueueItem
    items = (scoped(db, ProposalQueueItem, user)
             .filter(ProposalQueueItem.request_type == "buyer_request")
             .order_by(ProposalQueueItem.created_at.desc()).limit(100).all())
    from ..models import AuthTransaction
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    attempts = db.query(AuthTransaction).filter(AuthTransaction.user_id==user.id,AuthTransaction.kind=="send_attempt",AuthTransaction.payload["platform"].as_string()=="fiverr",AuthTransaction.payload["day"].as_string()==today).count()
    from ..send_budget import submission_cap
    cap = submission_cap("fiverr")
    return {"offers_remaining_today": max(0, cap-attempts) if cap > 0 else None,
            "drafts_remaining_today": fiverr_monitor.offers_remaining_today(user.id),
            "daily_limit": cap,
            "count": len(items)}


@router.post("/buyer-requests/process", response_model=dict)
def process_buyer_requests(body: dict, db: Session = Depends(get_db),
                           principal: User | None = Depends(get_worker_or_user)):
    """Stealth worker posts scraped buyer requests here for filtering + offers.
    Worker posts must carry `user_id` (from the stealth task payload)."""
    account_id = None
    if principal is None:
        source_task = _worker_result_task(db, body, "fetch_buyer_requests")
        account_id = _require_browser_account(db,source_task).id
        user_id = body.get("user_id")
        if not user_id:
            raise HTTPException(422, "user_id required for worker posts")
    else:
        user_id = principal.id
        if body.get("account_id") is not None:
            account = get_owned(db,PlatformAccount,body["account_id"],principal)
            if account is None or account.platform != "fiverr" or not account.enabled or account.mode == "disabled":
                raise HTTPException(404,"enabled Fiverr account not found")
            account_id = account.id
    requests = body.get("requests", [])
    if not isinstance(requests,list) or len(requests) > 100 or any(not isinstance(r,dict) for r in requests):
        raise HTTPException(422, "requests must contain at most 100 objects")
    return fiverr_monitor.process_buyer_requests(db, user_id, requests, account_id=account_id)


# --- stealth task handoff (browser worker polling) ---

@router.get("/stealth-session", response_model=dict)
def get_stealth_session(platform: str, user_id: int, db: Session = Depends(get_db),
                        worker: str = Depends(get_worker),
                        claim_token: str | None = Header(None, alias="X-Worker-Claim"),
                        worker_id: str | None = Header(None, alias="X-Worker-ID")):
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
                       StealthTask.status == "claimed",
                       StealthTask.claim_token == claim_token,
                       StealthTask.claimed_by == worker_id)
               .first())
    if claimed is None:
        raise HTTPException(409, "no claimed stealth task for this platform/user — "
                                 "sessions are only served while a task is executing")
    _check_claim(claimed, {"claim_token": claim_token, "worker_id": worker_id})
    account = _require_browser_account(db, claimed)
    db.add(AuditLog(user_id=user_id, action_type="stealth_session_read",
                    platform=platform,
                    detail={"task_id": claimed.id, "worker": worker}))
    db.commit()
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


def _task_out(t: StealthTask, *, include_claim: bool = False) -> dict:
    return {"id": t.id, "user_id": t.user_id, "platform": t.platform,
            "task_type": t.task_type, "payload": t.payload, "status": t.status,
            "claimed_by": t.claimed_by, "created_at": t.created_at,
            **({"claim_token": t.claim_token} if include_claim else {})}


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
    from sqlalchemy import case
    priority = case((StealthTask.task_type.in_(["submit_upwork_proposal", "submit_fiverr_offer", "create_gig_draft"]), 0), else_=1)
    return [_task_out(t) for t in q.order_by(priority, StealthTask.created_at).limit(50).all()]


@router.post("/stealth-tasks/{task_id}/claim", response_model=dict)
def claim_stealth_task(task_id: int, body: StealthTaskClaimIn,
                       db: Session = Depends(get_db),
                       worker: str = Depends(get_worker)):
    """Atomically claim a pending task so no two workers execute it."""
    if worker not in ("worker", "dev-worker") and body.worker_id != worker:
        raise HTTPException(403, "worker identity does not match the task claim")
    task = db.get(StealthTask, task_id)
    if task is None:
        raise HTTPException(404, "stealth task not found")
    if task.status != "pending":
        raise HTTPException(409, f"stealth task already {task.status}")
    _require_browser_account(db, task)
    now = datetime.now(timezone.utc)
    res = db.execute(
        update(StealthTask)
        .where(StealthTask.id == task_id, StealthTask.status == "pending")
        .values(status="claimed", claimed_by=body.worker_id, claimed_at=now,
                claim_token=secrets.token_urlsafe(32))
    )
    db.commit()
    if res.rowcount == 0:
        task = db.get(StealthTask, task_id)
        if task is None:
            raise HTTPException(404, "stealth task not found")
        raise HTTPException(409, f"stealth task already {task.status}")
    return _task_out(db.get(StealthTask, task_id), include_claim=True)


def _require_browser_account(db: Session, task: StealthTask):
    owner = db.get(User, task.user_id)
    from ..models import AuthTransaction
    if db.get(AuthTransaction,f"circuitstop:{task.user_id}:{task.platform}") is not None:
        raise HTTPException(409,"platform has a persistent manual stop")
    q = db.query(PlatformAccount).filter(
        PlatformAccount.user_id == task.user_id, PlatformAccount.platform == task.platform,
        PlatformAccount.enabled.is_(True), PlatformAccount.mode.in_(["stealth", "hybrid"]),
    )
    bound = (task.payload or {}).get("account_id")
    if bound is not None:
        q = q.filter(PlatformAccount.id == bound)
    accounts = q.limit(2).all()
    if owner is None or not owner.is_active or len(accounts) != 1:
        raise HTTPException(409, "bound account is missing, disabled, or ambiguous")
    epoch = (task.payload or {}).get("account_epoch")
    if epoch is not None and epoch != accounts[0].identity_epoch:
        raise HTTPException(409, "task belongs to a previous account identity; create new work for the current account")
    return accounts[0]


def _worker_result_task(db, body, expected_kind):
    task = db.query(StealthTask).filter_by(id=body.get("task_id")).populate_existing().with_for_update().one_or_none()
    if task is None or task.task_type != expected_kind:
        raise HTTPException(409,"result does not match an active task of the expected kind")
    _check_claim(task,body)
    _require_browser_account(db,task)
    if body.get("user_id") is not None and body["user_id"] != task.user_id:
        raise HTTPException(409,"result tenant does not match the task")
    if body.get("platform") is not None and body["platform"] != task.platform:
        raise HTTPException(409,"result platform does not match the task")
    return task


def _check_claim(task: StealthTask, body: dict):
    if (task.status != "claimed" or not task.claim_token
            or task.claimed_by != body.get("worker_id")
            or not secrets.compare_digest(task.claim_token, str(body.get("claim_token", "")))):
        raise HTTPException(409, "worker claim is invalid or no longer active")
    claimed_at = task.claimed_at
    if claimed_at is None:
        raise HTTPException(409, "worker claim has no expiry")
    if claimed_at.tzinfo is None:
        claimed_at = claimed_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - claimed_at >= timedelta(minutes=15):
        raise HTTPException(409, "worker claim expired")


@router.post("/stealth-tasks/{task_id}/authorize", response_model=dict)
def authorize_stealth_action(task_id: int, body: dict, db: Session = Depends(get_db),
                             worker: str = Depends(get_worker)):
    """Recheck the claim and account immediately before browser work/writes."""
    if worker not in ("worker", "dev-worker") and body.get("worker_id") != worker:
        raise HTTPException(403, "worker identity does not match the task claim")
    task = db.query(StealthTask).filter_by(id=task_id).populate_existing().with_for_update().one_or_none()
    if task is None:
        raise HTTPException(404, "stealth task not found")
    _check_claim(task, body)
    account = _require_browser_account(db, task)
    proposal_id = (task.payload or {}).get("proposal_queue_item_id")
    if proposal_id:
        from ..approval import require_snapshot
        from ..models import ProposalQueueItem
        proposal = db.query(ProposalQueueItem).filter_by(id=proposal_id, user_id=task.user_id).populate_existing().with_for_update().one_or_none()
        if proposal is None or proposal.status != "queued_for_browser":
            raise HTTPException(409, "proposal is no longer queued for this browser action")
        approved = require_snapshot(db, proposal)
        if approved.get("account_id") != account.id or task.payload.get("proposal_text") != approved["text"]:
            raise HTTPException(409, "task does not match the approved account and text")
        if task.task_type == "submit_upwork_proposal":
            expected = {
                "job_external_id": approved["job_external_id"],
                "job_url": approved["destination"],
                "bid_amount": approved["bid"],
                "on_behalf_of": approved["agency_member"],
                "connects_required": approved["connects_required"],
                "agency_id": (account.settings or {}).get("agency_id", ""),
            }
            if not expected["agency_id"] or any(task.payload.get(k) != v for k, v in expected.items()):
                raise HTTPException(409, "browser task identity, destination or price differs from review")
    # Reading state must not consume a half-open trial token.
    for scope in (None, task.user_id):
        if circuit_breaker.get_state(task.platform, scope, db=db).get("state") == "open":
            raise HTTPException(409, "platform automation is paused")
    from ..send_budget import reserve_send, reserve_circuit_trials
    reserve_send(db, task)
    reserve_circuit_trials(db, task)
    _check_claim(task, body)
    db.commit()
    return {"authorized": True, "task_id": task.id}


@router.post("/stealth-tasks/{task_id}/complete", response_model=dict)
async def complete_stealth_task(task_id: int, body: dict, db: Session = Depends(get_db),
                                worker: str = Depends(get_worker)):
    if worker not in ("worker", "dev-worker") and body.get("worker_id") != worker:
        raise HTTPException(403, "worker identity does not match the task claim")
    task = db.get(StealthTask, task_id)
    if not task:
        raise HTTPException(404, "stealth task not found")
    # only the claiming worker may complete its own task
    if task.status != "claimed":
        raise HTTPException(409, f"stealth task already {task.status}")
    if task.claimed_by != body.get("worker_id"):
        raise HTTPException(409, "stealth task claimed by another worker")
    if not isinstance(body.get("success"), bool):
        raise HTTPException(422, "success must be an explicit boolean")
    if not isinstance(body.get("result", {}), dict):
        raise HTTPException(422, "result must be an object")
    _check_claim(task, body)
    success = body["success"]
    now = datetime.now(timezone.utc)
    changed = db.execute(update(StealthTask).where(
        StealthTask.id == task.id, StealthTask.status == "claimed",
        StealthTask.claimed_by == body["worker_id"],
        StealthTask.claimed_at == task.claimed_at,
        StealthTask.claim_token == body["claim_token"],
    ).values(status="done" if success else "failed", result=body.get("result", {}),
             completed_at=now)).rowcount
    if not changed:
        db.rollback()
        raise HTTPException(409, "stealth task claim expired or was completed")
    db.refresh(task)
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
                user_id=task.user_id, db=db)
    from ..send_budget import finish_circuit_trials
    finish_circuit_trials(db, task, success)
    changed_item = _apply_submission_outcome(db, task, success)
    if (task.result or {}).get("session_expired"):
        _flag_session_expired(db, task)
    db.add(AuditLog(user_id=task.user_id, action_type="stealth_task_completed",
                    platform=task.platform,
                    detail={"task_id": task.id, "worker_id": task.claimed_by,
                            "success": success}))
    db.commit()
    if changed_item is not None:
        # after commit so a refetch triggered by the event sees the new status
        await alerts.broadcast(changed_item.user_id, {
            "type": "proposal_status_changed",
            "proposal_id": changed_item.id,
            "status": changed_item.status,
        })
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


def _apply_submission_outcome(db: Session, task: StealthTask,
                              success: bool) -> ProposalQueueItem | None:
    """Close the HITL loop for submission tasks: flip the review-queue item
    out of queued_for_browser and complete the agency handoff record.

    Returns the queue item when its status actually changed (so the caller can
    broadcast `proposal_status_changed` after commit), else None.

    The worker's explicit `result.submitted` verdict wins over the bare
    task-level success flag: a task can "succeed" (no crash) while the
    platform rejected the submit, or while the outcome could not be
    confirmed — the click already happened, so the latter becomes
    submitted_unverified for a human to check on the platform (NEVER
    auto-retried: a blind retry risks a duplicate proposal)."""
    item_id = (task.payload or {}).get("proposal_queue_item_id")
    if not item_id:
        return None
    result = task.result or {}
    submitted = result.get("submitted")
    flipped: ProposalQueueItem | None = None
    item = db.get(ProposalQueueItem, item_id)
    if item and item.user_id == task.user_id and item.platform == task.platform and item.status == "queued_for_browser":
        if not success:
            item.status = "failed" if submitted is False else "submitted_unverified"
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
        elif submitted is not True:
            item.status = "submitted_unverified"
            item.submission_result = {**(item.submission_result or {}), **result}
        else:
            item.status = "submitted"
        flipped = item
    if task.task_type == SUBMIT_UPWORK_PROPOSAL:
        from ..adapters.upwork_agency import UpworkAgencyAdapter
        # the agency handoff record is only closed on a CONFIRMED outcome —
        # an unverified submit stays pending for human reconciliation
        unverified = submitted is not True and submitted is not False
        if not unverified:
            UpworkAgencyAdapter(db, task.user_id).complete_submission(
                (task.payload or {}).get("job_external_id", ""),
                success and submitted is not False,
                note="stealth worker " +
                     ("submitted" if success and submitted is not False
                      else "failed"))
    return flipped


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

    if worker not in ("worker", "dev-worker") and body.get("worker_id") != worker:
        raise HTTPException(403, "worker identity does not match the task claim")
    task_id = body.get("task_id")
    results = body.get("results")
    if not task_id or not isinstance(results, list):
        raise HTTPException(422, "task_id and results (list) are required")
    task = db.query(StealthTask).filter(StealthTask.id == task_id).with_for_update().first()
    if task is None or task.task_type != SCRAPE_PROPOSAL_STATUS:
        raise HTTPException(404, "scrape_proposal_status task not found")
    _check_claim(task, body)
    allowed_ids = {entry.get("proposal_queue_item_id") for entry in (task.payload or {}).get("items", [])}
    for entry in results:
        if not isinstance(entry, dict) or entry.get("proposal_queue_item_id") not in allowed_ids:
            raise HTTPException(422, "result is not part of the claimed task")
        proposal = db.get(ProposalQueueItem, entry["proposal_queue_item_id"])
        if proposal is None or proposal.user_id != task.user_id or proposal.platform != task.platform:
            raise HTTPException(422, "result does not match task tenant/platform")
        approved_account = (proposal.approved_snapshot or {}).get("account_id") or proposal.platform_account_id
        if approved_account is not None and (task.payload or {}).get("account_id") != approved_account:
            raise HTTPException(422,"result does not match the proposal account")

    notifications = []
    summary = await apply_proposal_status_results(db, task, results, notifications)
    db.add(AuditLog(user_id=task.user_id, action_type="proposal_status_ingested",
                    platform=task.platform,
                    detail={"task_id": task.id, "results_count": len(results),
                            **summary}))
    if task.status == "claimed":
        task.status = "done"
        task.result = {"results_count": len(results), **summary}
        task.completed_at = datetime.now(timezone.utc)
    db.commit()
    for user_id, notification in notifications:
        await alerts.broadcast(user_id, notification)
    return {"task_id": task.id, "task_status": task.status, **summary}


# --- circuit breaker controls ---

@router.get("/circuit/{platform}", response_model=dict)
def circuit_state(platform: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from ..models import AuthTransaction
    global_state = circuit_breaker.get_state(platform, db=db)
    if global_state["state"] != "closed":
        return {**global_state, "global_stop": True}
    stop=db.get(AuthTransaction,f"circuitstop:{user.id}:{platform}")
    if stop is not None:
        return {"state":"open","manual_stop":True,"reason":stop.payload.get("reason","manual stop")}
    return circuit_breaker.get_state(platform,user.id,db=db)


@router.post("/circuit/{platform}",response_model=dict)
def set_circuit(platform: str, body: dict, user: User=Depends(get_current_user),db: Session=Depends(get_db)):
    from ..models import AuthTransaction
    if platform not in get_args(Platform):
        raise HTTPException(422,"unsupported platform")
    key=f"circuitstop:{user.id}:{platform}"
    owner=db.get(User,user.id)
    db.refresh(owner,with_for_update=True)
    row=db.get(AuthTransaction,key)
    state=body.get("state")
    reason=str(body.get("reason") or "manual")[:2000]
    if state=="open":
        if row is None:
            row=AuthTransaction(id=key,user_id=user.id,kind="circuit_stop",expires_at=datetime(9999,1,1,tzinfo=timezone.utc),payload={})
            db.add(row)
        row.payload={"reason":reason,"platform":platform}
    elif state=="closed":
        if row is not None:db.delete(row)
    else:
        raise HTTPException(400,"state must be 'open' or 'closed'")
    circuit_breaker.transition(platform,state,reason,user.id,manual_stop=state=="open",db=db)
    db.commit()
    return circuit_state(platform,user,db)


@router.post("/worker-heartbeat")
def worker_heartbeat(body: dict, worker: str = Depends(get_worker)):
    from ..cache import cache
    worker_id = body.get("worker_id")
    if not isinstance(worker_id,str) or not 1 <= len(worker_id) <= 100:
        raise HTTPException(422,"valid worker_id required")
    if worker not in ("worker","dev-worker") and worker_id != worker:
        raise HTTPException(403,"worker identity mismatch")
    platforms = body.get("platforms",[])
    if not isinstance(platforms,list) or any(p not in ("upwork","fiverr","guru","peopleperhour") for p in platforms):
        raise HTTPException(422,"unsupported worker platform")
    cache.set_json(f"worker:heartbeat:{worker_id}",{"at":datetime.now(timezone.utc).isoformat(),"platforms":platforms},ttl=180)
    return {"recorded":True}


@router.get("/worker-health")
def worker_health(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    from ..cache import cache
    ids = db.query(StealthTask.claimed_by).filter(StealthTask.user_id==user.id,StealthTask.claimed_by.isnot(None)).distinct().limit(100).all()
    return {"workers":[{"worker_id":worker_id,"heartbeat":cache.get_json(f"worker:heartbeat:{worker_id}")} for (worker_id,) in ids],
            "pending":db.query(StealthTask).filter_by(user_id=user.id,status="pending").count()}
