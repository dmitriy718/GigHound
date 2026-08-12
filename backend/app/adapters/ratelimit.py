"""Async rate limiting and retry utilities shared by all adapters."""
import asyncio
import logging
import random

import httpx

log = logging.getLogger(__name__)


class RateLimitExceeded(Exception):
    """Raised when a request would exceed the adapter's request budget."""


class AsyncRateLimiter:
    """Token-bucket rate limiter with a max-concurrency gate (request queueing)."""

    def __init__(self, rate_per_sec: float, max_concurrent: int = 4):
        self._interval = 1.0 / rate_per_sec
        self._lock = asyncio.Lock()
        self._next_at = 0.0
        self._sem = asyncio.Semaphore(max_concurrent)

    async def acquire(self):
        await self._sem.acquire()
        async with self._lock:
            loop = asyncio.get_event_loop()
            now = loop.time()
            wait = self._next_at - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_at = max(now, self._next_at) + self._interval

    def release(self):
        self._sem.release()


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
