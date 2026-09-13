"""Won-bid rate learning (Phase 3.4).

When an outcome is marked `hired`, the winning bid amount is recorded per
rate-card skill category (AdapterState key `rate_feedback:{category}`). Once
a category has ≥3 samples, `calculate_bid` nudges new estimates toward the
historical winning average, bounded to ±20% of the computed estimate.
"""
import logging
from datetime import datetime, timezone

import redis
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

_PLATFORM = "global"  # AdapterState requires a platform column; rate learning is cross-platform
_MAX_SAMPLES = 20
MIN_SAMPLES_FOR_NUDGE = 3


def _key(skill_category: str) -> str:
    return f"rate_feedback:{skill_category}"


def record_winning_bid(db: Session, user_id: int, skill_category: str, bid_amount: float) -> None:
    from .adapters.vault import StateStore
    from .cache import cache

    # The samples list is read-modify-write: serialize concurrent recorders
    # (two outcomes synced at once) so no sample is lost. Single writer per
    # tenant is the normal case — without Redis we accept the tiny race.
    lock = None
    if cache._r is not None:
        try:
            candidate = cache._r.lock(
                f"lock:rate_feedback:{user_id}:{skill_category}",
                timeout=10, blocking_timeout=5)
            if candidate.acquire():
                lock = candidate
        except redis.RedisError as exc:
            log.warning("rate-learning lock unavailable (%s); proceeding unlocked", exc)
    try:
        store = StateStore(db, user_id)
        data = store.get(_PLATFORM, _key(skill_category), {"samples": []})
        samples = list(data.get("samples") or [])
        samples.append({"bid_amount": float(bid_amount),
                        "at": datetime.now(timezone.utc).isoformat()})
        store.set(_PLATFORM, _key(skill_category), {"samples": samples[-_MAX_SAMPLES:]})
    finally:
        if lock is not None:
            try:
                lock.release()
            except redis.RedisError:
                pass


def winning_bid_samples(db: Session, user_id: int, skill_category: str, *, currency: str | None = None, job_type: str | None = None) -> list[dict]:
    # Only attributable, unit-compatible confirmed outcomes feed new estimates.
    # Legacy untyped aggregate samples are deliberately excluded.
    if not currency or not job_type:
        return []
    from .models import Job, ProposalQueueItem
    from .orchestrator import pick_rate
    rows = db.query(ProposalQueueItem, Job).join(Job, ProposalQueueItem.job_id == Job.id).filter(
        ProposalQueueItem.user_id == user_id, ProposalQueueItem.status == "submitted",
        ProposalQueueItem.outcome == "hired", ProposalQueueItem.outcome_at.isnot(None),
        Job.currency == currency, Job.job_type == job_type,
        ProposalQueueItem.bid_amount > 0,
    ).order_by(ProposalQueueItem.outcome_at.desc()).limit(100).all()
    result = []
    for proposal, job in rows:
        rate = pick_rate(db, user_id, job)
        if (rate.skill_category if rate else "general") == skill_category:
            result.append({"bid_amount": proposal.bid_amount, "proposal_id": proposal.id,
                           "currency": currency, "job_type": job_type})
    return result[:_MAX_SAMPLES]


def nudge_toward_wins(estimate: float, samples: list[dict]) -> tuple[float, str | None]:
    """Pull an estimate 50% toward the winning-bid average (≥3 samples),
    clamped to ±20% of the original estimate. Returns (amount, note)."""
    if estimate <= 0 or len(samples) < MIN_SAMPLES_FOR_NUDGE:
        return estimate, None
    avg = sum(s["bid_amount"] for s in samples) / len(samples)
    nudged = estimate + 0.5 * (avg - estimate)
    nudged = min(max(nudged, estimate * 0.8), estimate * 1.2)
    if abs(nudged - estimate) < 0.01:
        return estimate, None
    return nudged, f"nudged toward {len(samples)} past winning bids (avg {avg:,.0f} in matching currency/unit)"
