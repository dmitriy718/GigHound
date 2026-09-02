"""Stealth-task kinds and enqueue helpers (AD-4).

Canonical task kinds the browser worker understands. All backend enqueue
paths emit canonical kinds; the LEGACY_ALIASES map below exists only so
rows queued by pre-AD-4 code stay executable — no backend code emits
legacy type strings anymore.
"""
from sqlalchemy.orm import Session

from . import circuit_breaker
from .models import StealthTask

FETCH_BUYER_REQUESTS = "fetch_buyer_requests"
SCRAPE_GIG_METRICS = "scrape_gig_metrics"
SCRAPE_COMPETITORS = "scrape_competitors"  # reserved: no producer (P5-1)
CREATE_GIG_DRAFT = "create_gig_draft"
SUBMIT_UPWORK_PROPOSAL = "submit_upwork_proposal"
SUBMIT_FIVERR_OFFER = "submit_fiverr_offer"
SUBMIT_PROPOSAL = "submit_proposal"  # generic copy-assist platforms (PPH/Guru)
SCRAPE_PROPOSAL_STATUS = "scrape_proposal_status"  # read-only outcome/reply sync

ALL_KINDS = (
    FETCH_BUYER_REQUESTS, SCRAPE_GIG_METRICS, SCRAPE_COMPETITORS,
    CREATE_GIG_DRAFT, SUBMIT_UPWORK_PROPOSAL, SUBMIT_FIVERR_OFFER,
    SUBMIT_PROPOSAL, SCRAPE_PROPOSAL_STATUS,
)

# legacy task_type → canonical kind. No backend code emits these anymore —
# kept so in-flight rows queued by pre-AD-4 paths still resolve.
LEGACY_ALIASES = {
    "fiverr_fetch_buyer_requests": FETCH_BUYER_REQUESTS,
    "gig_scrape_metrics": SCRAPE_GIG_METRICS,
    "competitor_scrape": SCRAPE_COMPETITORS,
    "fiverr_create_gig": CREATE_GIG_DRAFT,
    "upwork_catalog_upsert": CREATE_GIG_DRAFT,
    "fiverr_send_offer": SUBMIT_FIVERR_OFFER,
}


def canonical_kind(task_type: str) -> str:
    return LEGACY_ALIASES.get(task_type, task_type)


def enqueue_stealth_task(db: Session, user_id: int, platform: str,
                         task_type: str, payload: dict) -> StealthTask:
    """Create a pending stealth task, honoring the platform circuit breaker.

    When the circuit is open the row is recorded as skipped_circuit_open so
    the kill switch is visible in the UI instead of silently dropping work.
    """
    allowed, reason = circuit_breaker.check(platform, user_id)
    task = StealthTask(
        user_id=user_id, platform=platform, task_type=task_type,
        payload=payload,
        status="pending" if allowed else "skipped_circuit_open",
        result={} if allowed else {"reason": reason},
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task
