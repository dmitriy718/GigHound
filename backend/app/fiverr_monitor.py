"""Fiverr buyer-request monitor + gig creation queueing.

The stealth-browser worker does the actual site interaction; this module owns
the logic: what to fetch, how to filter, how to respond, and the hard
platform caps (10 offers/day, 1 gig draft/hour).

Daily offer counter lives in Redis (key resets at UTC midnight).
"""
import logging
import time
from datetime import datetime, timezone

from rapidfuzz import fuzz
from sqlalchemy.orm import Session

from . import circuit_breaker
from .cache import cache
from .models import (AuditLog, GigTemplate, Job, ProposalQueueItem, StealthTask)
from .schemas import ClientInfo

log = logging.getLogger(__name__)

FIVERR_DAILY_OFFER_LIMIT = 10
GIG_DRAFTS_PER_HOUR = 1

_local_counters: dict[str, tuple[int, float]] = {}


def _counter(key: str, window_sec: float) -> int:
    """Increment-and-get a counter with a rolling window (Redis or local)."""
    if cache._r is not None:
        n = cache._r.incr(key)
        if n == 1:
            cache._r.expire(key, int(window_sec))
        return n
    count, reset_at = _local_counters.get(key, (0, time.time() + window_sec))
    if time.time() > reset_at:
        count, reset_at = 0, time.time() + window_sec
    count += 1
    _local_counters[key] = (count, reset_at)
    return count


def _peek(key: str) -> int:
    if cache._r is not None:
        v = cache._r.get(key)
        return int(v) if v else 0
    return _local_counters.get(key, (0, 0))[0]


def offers_remaining_today() -> int:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return max(0, FIVERR_DAILY_OFFER_LIMIT - _peek(f"fiverr:offers:{day}"))


# ---------------- gig creation ----------------

def queue_gig_creation(db: Session, template: GigTemplate) -> tuple[StealthTask | None, str]:
    """Queue a fiverr_create_gig stealth task (DRAFT only — never auto-publish).

    Returns (task, error). Enforces circuit breaker + 1 draft/hour cap.
    """
    allowed, reason = circuit_breaker.check(template.platform)
    if not allowed:
        return None, reason
    count = _counter(f"gigdraft:{template.platform}", 3600)
    if count > GIG_DRAFTS_PER_HOUR:
        return None, f"gig draft rate limit: {GIG_DRAFTS_PER_HOUR}/hour per platform"

    task = StealthTask(
        platform=template.platform,
        task_type=f"{template.platform}_create_gig" if template.platform != "fiverr" else "fiverr_create_gig",
        payload={
            "template_id": template.id,
            "template": template.template_json,
            "save_as_draft": True,  # hard rule: never auto-publish
            "gallery": template.template_json.get("gallery", []),
        },
    )
    db.add(task)
    db.add(AuditLog(action_type="gig_created", platform=template.platform, detail={
        "template_id": template.id, "stealth_task_id": None, "mode": "draft",
    }))
    db.commit()
    db.refresh(task)
    return task, ""


def queue_upwork_catalog_upsert(db: Session, template: GigTemplate) -> tuple[StealthTask | None, str]:
    """Upwork Project Catalog: text fields go via API where available; image
    upload + publish step are stealth tasks. auto_publish respected per template,
    defaulting to draft-for-review."""
    allowed, reason = circuit_breaker.check("upwork")
    if not allowed:
        return None, reason
    task = StealthTask(
        platform="upwork",
        task_type="upwork_catalog_upsert",
        payload={
            "template_id": template.id,
            "template": template.template_json,
            "publish": bool(template.auto_publish),  # default False → draft
        },
    )
    db.add(task)
    db.add(AuditLog(action_type="gig_created", platform="upwork", detail={
        "template_id": template.id, "auto_publish": bool(template.auto_publish),
    }))
    db.commit()
    db.refresh(task)
    return task, ""


# ---------------- buyer request monitor ----------------

def matching_buyer_requests(db: Session, requests: list[dict]) -> list[dict]:
    """Filter raw buyer requests against active gig templates' categories/tags."""
    templates = (db.query(GigTemplate)
                 .filter(GigTemplate.platform == "fiverr", GigTemplate.is_active.is_(True))
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


def process_buyer_requests(db: Session, requests: list[dict]) -> dict:
    """Monitor tick: filter → generate offers → queue for approval.

    ALWAYS requires human approval (no auto-approve for buyer requests).
    Stops when the 10-offers/day platform cap is hit.
    """
    allowed, reason = circuit_breaker.check("fiverr")
    if not allowed:
        log.warning("buyer request monitor skipped: %s", reason)
        return {"queued": 0, "skipped_reason": reason}

    matched = matching_buyer_requests(db, requests)
    queued = 0
    for req in matched:
        if offers_remaining_today() <= 0:
            log.info("fiverr daily offer cap reached (%d/day)", FIVERR_DAILY_OFFER_LIMIT)
            break
        ext_id = str(req.get("id") or req.get("url") or req.get("title", "")[:80])
        exists = (db.query(Job)
                  .filter(Job.platform == "fiverr", Job.external_id == ext_id)
                  .first())
        if exists:
            continue
        job = Job(
            external_id=ext_id, platform="fiverr",
            title=req.get("title", "Buyer request"),
            description=req.get("description", ""),
            url=req.get("url", ""),
            job_type="gig",
            budget_min=req.get("budget"), budget_max=req.get("budget"),
            currency=req.get("currency", "USD"),
            client_info=ClientInfo().model_dump(),
            quality_score=60.0,  # buyer requests: fixed neutral score
            score_breakdown={"source": "buyer_request_monitor"},
            status="new",
        )
        db.add(job)
        db.flush()
        offer_text = generate_custom_offer(req)
        item = ProposalQueueItem(
            job_id=job.id, platform="fiverr", request_type="buyer_request",
            proposal_text=offer_text, humanized_text=offer_text,
            bid_amount=req.get("budget") or 50,
            bid_period_days=3,
            bid_rationale="buyer request custom offer",
            status="pending_review",
            confidence=60.0,
        )
        db.add(item)
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _counter(f"fiverr:offers:{day}", 86400)
        db.add(AuditLog(action_type="buyer_request_sent", platform="fiverr", detail={
            "job_id": job.id, "offer": offer_text[:200], "status": "queued_for_approval",
        }))
        queued += 1
    db.commit()
    return {"queued": queued, "offers_remaining": offers_remaining_today()}


def enqueue_buyer_request_fetch(db: Session) -> StealthTask | None:
    """Queue the stealth fetch task the browser worker executes each tick."""
    allowed, reason = circuit_breaker.check("fiverr")
    if not allowed:
        log.warning("buyer request fetch skipped: %s", reason)
        task = StealthTask(platform="fiverr", task_type="fiverr_fetch_buyer_requests",
                           payload={}, status="skipped_circuit_open",
                           result={"reason": reason})
        db.add(task)
        db.commit()
        return None
    task = StealthTask(platform="fiverr", task_type="fiverr_fetch_buyer_requests", payload={})
    db.add(task)
    db.commit()
    db.refresh(task)
    return task
