"""Fiverr buyer-request monitor + gig creation queueing.

The stealth-browser worker does the actual site interaction; this module owns
the logic: what to fetch, how to filter, how to respond, and the hard
platform caps (10 offers/day, 1 gig draft/hour).

Daily offer counter lives in Redis (key resets at UTC midnight).
"""
import logging
import time
from datetime import datetime, timezone

import redis
from rapidfuzz import fuzz
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import circuit_breaker
from .auth import platform_account_settings
from .cache import cache
from .models import (AuditLog, GigTemplate, Job, ProposalQueueItem, StealthTask)
from .schemas import ClientInfo
from .stealth import CREATE_GIG_DRAFT, FETCH_BUYER_REQUESTS

log = logging.getLogger(__name__)

FIVERR_DAILY_OFFER_LIMIT = 10
GIG_DRAFTS_PER_HOUR = 1

_local_counters: dict[str, tuple[int, float]] = {}


def _counter(key: str, window_sec: float) -> int | None:
    """Increment-and-get a counter with a rolling window (Redis or local).

    Returns None when Redis errors mid-run: the cap cannot be enforced, so
    callers must skip the gated action conservatively (never send offers or
    create gigs when the limit can't be verified)."""
    if cache._r is not None:
        try:
            n = cache._r.incr(key)
            if n == 1:
                cache._r.expire(key, int(window_sec))
        except redis.RedisError as exc:
            log.warning("Redis counter unavailable (%s); cap cannot be enforced", exc)
            return None
        return n
    count, reset_at = _local_counters.get(key, (0, time.time() + window_sec))
    if time.time() > reset_at:
        count, reset_at = 0, time.time() + window_sec
    count += 1
    _local_counters[key] = (count, reset_at)
    return count


def _peek(key: str) -> int:
    if cache._r is not None:
        try:
            v = cache._r.get(key)
        except redis.RedisError:
            return 0  # display-only path; degrade to "nothing used"
        return int(v) if v else 0
    return _local_counters.get(key, (0, 0))[0]


def _offers_key(user_id: int) -> str:
    """Per-tenant daily counter key — the 10/day cap is per user, not global."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"fiverr:offers:{user_id}:{day}"


def offers_remaining_today(user_id: int) -> int:
    return max(0, FIVERR_DAILY_OFFER_LIMIT - _peek(_offers_key(user_id)))


# ---------------- gig creation ----------------

def queue_gig_creation(db: Session, template: GigTemplate) -> tuple[StealthTask | None, str]:
    """Queue a create_gig_draft stealth task (DRAFT only — never auto-publish).

    Returns (task, error). Enforces circuit breaker + 1 draft/hour cap.
    Only fiverr gig drafts are supported by the worker.
    """
    if template.platform != "fiverr":
        log.info("gig creation skipped for platform %s: only fiverr gig drafts "
                 "are supported by the worker", template.platform)
        return None, f"gig creation not supported for '{template.platform}'"
    allowed, reason = circuit_breaker.check(template.platform, template.user_id)
    if not allowed:
        return None, reason
    count = _counter(f"gigdraft:{template.platform}:{template.user_id}", 3600)
    if count is None:
        return None, "gig draft rate limit cannot be enforced (Redis down) — try again"
    if count > GIG_DRAFTS_PER_HOUR:
        return None, f"gig draft rate limit: {GIG_DRAFTS_PER_HOUR}/hour per account"

    task = StealthTask(
        user_id=template.user_id,
        platform=template.platform,
        task_type=CREATE_GIG_DRAFT,
        payload={
            "template_id": template.id,
            "template": template.template_json,
            "save_as_draft": True,  # hard rule: never auto-publish
            "gallery": template.template_json.get("gallery", []),
        },
    )
    db.add(task)
    db.add(AuditLog(user_id=template.user_id, action_type="gig_created", platform=template.platform, detail={
        "template_id": template.id, "stealth_task_id": None, "mode": "draft",
    }))
    db.commit()
    db.refresh(task)
    return task, ""


def queue_upwork_catalog_upsert(db: Session, template: GigTemplate) -> tuple[StealthTask | None, str]:
    """Upwork Project Catalog drafts are not supported by the worker yet (no
    upwork gig-form config) — log and skip cleanly instead of queueing a
    stealth task that would fail 100% of the time."""
    allowed, reason = circuit_breaker.check("upwork", template.user_id)
    if not allowed:
        return None, reason
    log.info("upwork catalog upsert skipped for template %d (user %d): "
             "catalog drafts aren't supported by the worker yet",
             template.id, template.user_id)
    return None, "upwork catalog drafts aren't supported by the worker yet"


# ---------------- buyer request monitor ----------------

def matching_buyer_requests(db: Session, user_id: int, requests: list[dict]) -> list[dict]:
    """Filter raw buyer requests against active gig templates' categories/tags."""
    templates = (db.query(GigTemplate)
                 .filter(GigTemplate.user_id == user_id,
                         GigTemplate.platform == "fiverr", GigTemplate.is_active.is_(True))
                 .all())
    if not templates:
        return []
    keywords = set()
    for tpl in templates:
        tj = tpl.template_json
        keywords.update(t.lower() for t in (tj.get("tags") or []))
        if tj.get("subcategory"):
            keywords.add(tj["subcategory"].lower())
        if tj.get("category"):
            keywords.add(tj["category"].lower())
    matched = []
    for req in requests:
        hay = f"{req.get('title', '')} {req.get('description', '')} {req.get('category', '')}".lower()
        if any(fuzz.partial_ratio(k, hay) >= 70 for k in keywords):
            matched.append(req)
    return matched


def generate_custom_offer(request: dict, price: float | None = None,
                          turnaround_days: int = 3) -> str:
    """Ultra-brief Fiverr offer: 2-3 sentences, price + turnaround."""
    title = (request.get("title") or "your project")[:60]
    budget = request.get("budget")
    offer = price or budget or 50
    return (f"I can handle \"{title}\" — done similar work this month. "
            f"Custom offer: ${offer:g}, delivered in {turnaround_days} days. "
            f"Happy to share a relevant sample before you decide.")


def process_buyer_requests(db: Session, user_id: int, requests: list[dict]) -> dict:
    """Monitor tick: filter → generate offers → queue for approval.

    ALWAYS requires human approval (no auto-approve for buyer requests).
    Stops when the 10-offers/day platform cap is hit.
    """
    allowed, reason = circuit_breaker.check("fiverr", user_id)
    if not allowed:
        log.warning("buyer request monitor skipped: %s", reason)
        return {"queued": 0, "skipped_reason": reason}

    matched = matching_buyer_requests(db, user_id, requests)
    queued = 0
    for req in matched:
        ext_id = str(req.get("id") or req.get("url") or req.get("title", "")[:80])
        exists = (db.query(Job)
                  .filter(Job.user_id == user_id,
                          Job.platform == "fiverr", Job.external_id == ext_id)
                  .first())
        if exists:
            continue
        # the atomic INCR IS the cap check — a check-then-increment peek would
        # let two concurrent runs both pass and exceed the 10/day ToS cap.
        # (Over-counts slightly when a queued offer later fails to flush —
        # cosmetic only.)
        count = _counter(_offers_key(user_id), 86400)
        if count is None:
            log.warning("offer cap cannot be enforced (Redis down); skipping "
                        "buyer request cycle for user %d", user_id)
            break
        if count > FIVERR_DAILY_OFFER_LIMIT:
            log.info("fiverr daily offer cap reached (%d/day, user %d)",
                     FIVERR_DAILY_OFFER_LIMIT, user_id)
            break
        job = Job(
            user_id=user_id,
            external_id=ext_id, platform="fiverr",
            title=req.get("title", "Buyer request"),
            description=req.get("description", ""),
            url=req.get("url", ""),
            job_type="gig",
            budget_min=req.get("budget"), budget_max=req.get("budget"),
            # Job.currency is NOT NULL — the worker sends None when it can't
            # detect the currency, so fall back to the model default ("USD")
            currency=req.get("currency") or "USD",
            client_info=ClientInfo().model_dump(),
            quality_score=60.0,  # buyer requests: fixed neutral score
            score_breakdown={"source": "buyer_request_monitor"},
            status="new",
        )
        db.add(job)
        try:
            db.flush()
        except IntegrityError:
            # unique (user_id, platform, external_id) — a concurrent monitor
            # run posted the same request after our exists check
            db.rollback()
            continue
        offer_text = generate_custom_offer(req)
        item = ProposalQueueItem(
            user_id=user_id,
            job_id=job.id, platform="fiverr", request_type="buyer_request",
            proposal_text=offer_text, humanized_text=offer_text,
            bid_amount=req.get("budget") or 50,
            bid_period_days=3,
            bid_rationale="buyer request custom offer",
            status="pending_review",
            confidence=60.0,
        )
        db.add(item)
        db.add(AuditLog(user_id=user_id, action_type="buyer_request_sent", platform="fiverr", detail={
            "job_id": job.id, "offer": offer_text[:200], "status": "queued_for_approval",
        }))
        # commit per request so an IntegrityError rollback above only drops
        # the duplicate, never the offers already queued this cycle
        db.commit()
        queued += 1
    return {"queued": queued, "offers_remaining": offers_remaining_today(user_id)}


def enqueue_buyer_request_fetch(db: Session, user_id: int) -> StealthTask | None:
    """Queue the stealth fetch task the browser worker executes each tick.

    The payload carries the tenant's fiverr seller username (account
    settings): the worker scrapes /users/{username}/briefs, and without it
    the task is doomed to scrape /users/me — skip enqueueing instead of
    generating a failure that feeds the circuit breaker.
    """
    allowed, reason = circuit_breaker.check("fiverr", user_id)
    if not allowed:
        log.warning("buyer request fetch skipped: %s", reason)
        task = StealthTask(user_id=user_id, platform="fiverr", task_type=FETCH_BUYER_REQUESTS,
                           payload={}, status="skipped_circuit_open",
                           result={"reason": reason})
        db.add(task)
        db.commit()
        return None
    username = platform_account_settings(db, user_id, "fiverr").get("username")
    if not username:
        log.warning("buyer request fetch skipped for user %d: no fiverr seller "
                    "username configured (account settings)", user_id)
        return None
    task = StealthTask(user_id=user_id, platform="fiverr", task_type=FETCH_BUYER_REQUESTS,
                       payload={"username": username})
    db.add(task)
    db.commit()
    db.refresh(task)
    return task
