"""Won-bid rate learning (Phase 3.4).

When an outcome is marked `hired`, the winning bid amount is recorded per
rate-card skill category (AdapterState key `rate_feedback:{category}`). Once
a category has ≥3 samples, `calculate_bid` nudges new estimates toward the
historical winning average, bounded to ±20% of the computed estimate.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

_PLATFORM = "global"  # AdapterState requires a platform column; rate learning is cross-platform
_MAX_SAMPLES = 20
MIN_SAMPLES_FOR_NUDGE = 3


def _key(skill_category: str) -> str:
    return f"rate_feedback:{skill_category}"


def record_winning_bid(db: Session, user_id: int, skill_category: str, bid_amount: float) -> None:
    from .adapters.vault import StateStore

    store = StateStore(db, user_id)
    data = store.get(_PLATFORM, _key(skill_category), {"samples": []})
    samples = list(data.get("samples") or [])
    samples.append({"bid_amount": float(bid_amount),
                    "at": datetime.now(timezone.utc).isoformat()})
    store.set(_PLATFORM, _key(skill_category), {"samples": samples[-_MAX_SAMPLES:]})


def winning_bid_samples(db: Session, user_id: int, skill_category: str) -> list[dict]:
    from .adapters.vault import StateStore

    data = StateStore(db, user_id).get(_PLATFORM, _key(skill_category), {"samples": []})
    return [s for s in (data.get("samples") or [])
            if isinstance(s, dict) and s.get("bid_amount")]


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
    return nudged, f"nudged toward {len(samples)} past winning bids (avg ${avg:,.0f})"
