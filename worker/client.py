"""HTTP client for the GigHound backend stealth-task protocol.

Handles worker-token auth, retries with exponential backoff on transient
errors (network, 5xx), and typed task/result payloads.
"""
import logging
import time

import httpx
from pydantic import BaseModel

log = logging.getLogger(__name__)


class StealthTaskOut(BaseModel):
    id: int
    user_id: int
    platform: str
    task_type: str
    payload: dict = {}
    status: str = "pending"


class ClaimConflictError(Exception):
    """Another worker claimed the task first (HTTP 409)."""


class BackendError(Exception):
    """Non-retryable backend error (4xx other than claim conflict)."""


class WorkerClient:
    def __init__(self, api_url: str, worker_token: str, worker_id: str,
                 max_retries: int = 3, timeout: float = 30.0,
                 client: httpx.Client | None = None):
        self.worker_id = worker_id
        self.max_retries = max_retries
        self._client = client or httpx.Client(
            base_url=api_url.rstrip("/"),
            headers={"Authorization": f"Bearer {worker_token}"},
            timeout=timeout,
        )

    def close(self):
        self._client.close()

    # ---------------- transport with retry/backoff ----------------

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        delay = 1.0
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self._client.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                if attempt == self.max_retries:
                    raise
                log.warning("backend unreachable (%s), retry %d/%d in %.0fs",
                            exc, attempt, self.max_retries, delay)
                time.sleep(delay)
                delay *= 2
                continue
            if resp.status_code == 409:
                raise ClaimConflictError(f"{method} {path}: {resp.text}")
            if resp.status_code >= 500:
                if attempt == self.max_retries:
                    resp.raise_for_status()
                log.warning("backend %d, retry %d/%d in %.0fs",
                            resp.status_code, attempt, self.max_retries, delay)
                time.sleep(delay)
                delay *= 2
                continue
            if resp.status_code >= 400:
                raise BackendError(f"{method} {path}: {resp.status_code} {resp.text}")
            return resp
        raise BackendError(f"{method} {path}: retries exhausted")  # unreachable

    # ---------------- protocol ----------------

    def poll_tasks(self, platform: str, status: str = "pending") -> list[StealthTaskOut]:
        resp = self._request("GET", "/api/gigs/stealth-tasks",
                             params={"platform": platform, "status": status})
        return [StealthTaskOut.model_validate(t) for t in resp.json()]

    def get_stealth_session(self, platform: str, user_id: int) -> dict:
        """Fetch the vault-enrolled browser session for (platform, user_id).

        Returns {"storage_state": dict|None, "credentials_present": bool} —
        the storage_state (when enrolled via the Accounts UI) lets the worker
        seed its browser context without the CLI login flow.
        """
        resp = self._request("GET", "/api/gigs/stealth-session",
                             params={"platform": platform, "user_id": user_id})
        return resp.json()

    def claim_task(self, task_id: int) -> StealthTaskOut:
        resp = self._request("POST", f"/api/gigs/stealth-tasks/{task_id}/claim",
                             json={"worker_id": self.worker_id})
        return StealthTaskOut.model_validate(resp.json())

    def complete_task(self, task_id: int, success: bool, result: dict) -> dict:
        resp = self._request("POST", f"/api/gigs/stealth-tasks/{task_id}/complete",
                             json={"worker_id": self.worker_id,
                                   "success": success, "result": result})
        return resp.json()

    # ---------------- result posting ----------------

    def post_buyer_requests(self, user_id: int, requests: list[dict]) -> dict:
        resp = self._request("POST", "/api/gigs/buyer-requests/process",
                             json={"user_id": user_id, "requests": requests})
        return resp.json()

    def post_metrics(self, gig_id: int, impressions: int, clicks: int,
                     orders: int, revenue: float, week: str | None = None) -> dict:
        resp = self._request("POST", "/api/gigs/metrics", json={
            "gig_id": gig_id, "impressions": impressions, "clicks": clicks,
            "orders": orders, "revenue": revenue, "week": week,
        })
        return resp.json()

    def post_competitors(self, user_id: int, platform: str, category: str,
                         gigs: list[dict], my_price: float | None = None) -> dict:
        resp = self._request("POST", "/api/gigs/competitors", json={
            "user_id": user_id, "platform": platform, "category": category,
            "gigs": gigs, "my_price": my_price,
        })
        return resp.json()

    def post_proposal_status(self, task_id: int, results: list[dict]) -> dict:
        """Post scrape_proposal_status results; the backend applies outcome /
        reply mappings and completes the task."""
        resp = self._request("POST", "/api/gigs/proposal-status",
                             json={"task_id": task_id, "results": results})
        return resp.json()
