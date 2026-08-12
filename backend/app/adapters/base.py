"""Adapter base class and shared exceptions."""
import abc
import logging

import httpx

from .ratelimit import AsyncRateLimiter, request_with_retry
from .schema import JobPosting

log = logging.getLogger(__name__)


class AdapterError(Exception):
    pass


class AdapterAuthError(AdapterError):
    pass


class QuotaDepletedError(AdapterError):
    """Raised when a platform action quota (e.g. monthly bids) is exhausted."""


class PlatformAdapter(abc.ABC):
    platform: str = ""
    rate_per_sec: float = 2.0

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=30.0)
        self.limiter = AsyncRateLimiter(self.rate_per_sec)

    async def close(self):
        if self._owns_client:
            await self.client.aclose()

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        return await request_with_retry(self.client, method, url, limiter=self.limiter, **kwargs)

    @abc.abstractmethod
    async def search_jobs(self, query: str = "", **kwargs) -> list[JobPosting]:
        ...

    @abc.abstractmethod
    async def get_job_details(self, external_id: str) -> JobPosting:
        ...

    async def health_check(self) -> bool:
        return True
