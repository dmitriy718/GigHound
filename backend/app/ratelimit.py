"""Inbound HTTP rate limiting: per-user Redis token buckets.

Thin shared helper over the cache client, in the style of the ingest limiter
(routers/jobs.py). Fails open when Redis is down — a limiter must never block
legitimate traffic. (Outbound platform pacing is a different concern; see
adapters/ratelimit.py.)
"""
import logging

from fastapi import HTTPException

from .cache import cache
from .models import User

log = logging.getLogger(__name__)

# LLM-cost generation endpoints (proposal/profile template generation, gig
# FAQs, follow-ups, interview prep) share one per-user bucket.
LLM_GEN_RATE_LIMIT = 20        # generations per window per user
LLM_GEN_WINDOW_SECONDS = 3600


def check_user_rate(user: User, bucket: str, limit: int, window_seconds: int,
                    detail: str) -> None:
    """429 when the user's bucket overflows; graceful no-op if Redis is down."""
    if cache._r is None:
        return
    key = f"{bucket}:{user.id}"
    try:
        hits = cache._r.incr(key)
        if hits == 1:
            cache._r.expire(key, window_seconds)
    except Exception:  # noqa: BLE001 — the limiter must never block the request
        log.warning("%s rate limiter unavailable; skipping", bucket)
        return
    if hits > limit:
        raise HTTPException(429, detail)


def check_llm_gen_rate(user: User) -> None:
    """Shared per-user budget for the LLM-cost generation endpoints."""
    check_user_rate(user, "llm_gen", LLM_GEN_RATE_LIMIT, LLM_GEN_WINDOW_SECONDS,
                    f"generation rate limit exceeded ({LLM_GEN_RATE_LIMIT}/hour)")
