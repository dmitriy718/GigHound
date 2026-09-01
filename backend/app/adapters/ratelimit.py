"""Async rate limiting, retry, and daily action budgets shared by all adapters.

Rate limiters are shared per (platform, principal) through a module-level
registry (`get_limiter`), so pacing survives across the per-request adapter
instances built by routers and Celery tasks. The pacing interval is jittered
±30% per acquire to avoid metronomic traffic.

Daily action budgets cap platform-touching actions (searches, submissions)
per (platform, principal): a Redis counter `rl:{platform}:{principal}:{date}`
compared against `GIGHOUND_DAILY_CAP_{PLATFORM}` (unset = unlimited). When
Redis is down the budget check is a graceful no-op — pacing still applies
via the shared limiter.
"""
import asyncio
import logging
import os
import random
import threading
import weakref
from datetime import datetime, timezone

import httpx
import redis

from ..cache import cache

log = logging.getLogger(__name__)


class RateLimitExceeded(Exception):
    """Raised when a request would exceed the adapter's request budget."""


class DailyBudgetExceeded(RateLimitExceeded):
    """Raised when a (platform, principal) pair exhausts its daily action budget."""


class AsyncRateLimiter:
    """Token-bucket rate limiter with a max-concurrency gate (request queueing).

    State is kept per event loop so one shared instance stays safe to reuse
    across loops (pytest runs each async test on a fresh loop; Celery tasks
    call asyncio.run) while pacing remains shared within a process.
    """

    def __init__(self, rate_per_sec: float, max_concurrent: int = 4, jitter: float = 0.3):
        self._interval = 1.0 / rate_per_sec
        self._jitter = jitter
        self._max_concurrent = max_concurrent
        self._loops: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

    def _state(self) -> dict:
        loop = asyncio.get_running_loop()
        state = self._loops.get(loop)
        if state is None:
            state = {
                "lock": asyncio.Lock(),
                "sem": asyncio.Semaphore(self._max_concurrent),
                "next_at": 0.0,
            }
            self._loops[loop] = state
        return state

    async def acquire(self):
        state = self._state()
        await state["sem"].acquire()
        async with state["lock"]:
            now = asyncio.get_running_loop().time()
            wait = state["next_at"] - now
            if wait > 0:
                await asyncio.sleep(wait)
            jittered = self._interval * random.uniform(1 - self._jitter, 1 + self._jitter)
            state["next_at"] = max(now, state["next_at"]) + jittered

    def release(self):
        self._state()["sem"].release()


# --- shared limiter registry (Phase 4.5) ---

_limiters: dict[tuple[str, str], AsyncRateLimiter] = {}
_limiters_lock = threading.Lock()


def get_limiter(platform: str, principal: str, rate_per_sec: float,
                max_concurrent: int = 4) -> AsyncRateLimiter:
    """Shared limiter per (platform, principal) — cross-request pacing."""
    key = (platform, principal)
    with _limiters_lock:
        limiter = _limiters.get(key)
        if limiter is None:
            limiter = AsyncRateLimiter(rate_per_sec, max_concurrent)
            _limiters[key] = limiter
        return limiter


# --- daily action budget (Phase 4.5) ---

def daily_cap(platform: str) -> int | None:
    """Configured daily action cap for a platform; None = unlimited."""
    raw = os.getenv(f"GIGHOUND_DAILY_CAP_{platform.upper()}", "")
    if not raw:
        return None
    try:
        cap = int(raw)
    except ValueError:
        log.warning("invalid GIGHOUND_DAILY_CAP_%s=%r — ignoring", platform.upper(), raw)
        return None
    return cap if cap > 0 else None


def consume_daily_action(platform: str, principal: str) -> int:
    """Count one platform action against today's budget; return actions used.

    Returns 0 (no-op) when no cap is configured or Redis is unavailable.
    Raises DailyBudgetExceeded when the cap is already reached.
    """
    cap = daily_cap(platform)
    if cap is None or cache._r is None:
        return 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key = f"rl:{platform}:{principal}:{today}"
    try:
        used = cache._r.incr(key)
        if used == 1:
            cache._r.expire(key, 48 * 3600)
    except redis.RedisError as exc:
        # graceful no-op per module docstring: Redis down → no budget
        # enforcement, pacing via the shared limiter still applies
        log.warning("Redis unavailable (%s); daily budget check skipped", exc)
        return 0
    if used > cap:
        raise DailyBudgetExceeded(
            f"{platform}: daily action budget of {cap} reached for '{principal}'; "
            "paused until tomorrow (UTC)"
        )
    return used


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    limiter: AsyncRateLimiter | None = None,
    max_attempts: int = 5,
    retry_statuses: tuple[int, ...] = (429, 500, 502, 503, 504),
    **kwargs,
) -> httpx.Response:
    """HTTP request with exponential backoff + jitter on 429/5xx.

    Honors `Retry-After` when present. Raises the final httpx.HTTPStatusError
    if all attempts are exhausted.
    """
    delay = 1.0
    for attempt in range(1, max_attempts + 1):
        if limiter:
            await limiter.acquire()
        try:
            resp = await client.request(method, url, **kwargs)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            if attempt == max_attempts:
                raise
            log.warning("transport error (%s), retry %d/%d in %.1fs", exc, attempt, max_attempts, delay)
            await asyncio.sleep(delay + random.uniform(0, 0.5))
            delay = min(delay * 2, 60)
            continue
        finally:
            if limiter:
                limiter.release()

        if resp.status_code not in retry_statuses:
            resp.raise_for_status()
            return resp

        if attempt == max_attempts:
            resp.raise_for_status()
        retry_after = resp.headers.get("Retry-After")
        wait = float(retry_after) if retry_after and retry_after.isdigit() else delay
        log.warning("HTTP %d from %s, retry %d/%d in %.1fs", resp.status_code, url, attempt, max_attempts, wait)
        await asyncio.sleep(wait + random.uniform(0, 0.5))
        delay = min(delay * 2, 60)

    raise RuntimeError("unreachable")  # pragma: no cover
