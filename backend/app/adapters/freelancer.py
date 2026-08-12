"""Freelancer.com official API adapter (OAuth 2.0).

Docs: https://developers.freelancer.com
Covers: project search, project details, bid placement, bid status,
user info, message threads — plus local monthly bid-quota tracking.

Credentials expected in the vault under principal "default":
    {
      "client_id": "...", "client_secret": "...",
      "access_token": "...", "refresh_token": "...",
      "expires_at": "2026-08-08T13:00:00+00:00"
    }
Obtain the initial tokens via the authorization-code flow (see
`build_authorize_url` / `exchange_code`).
"""
import logging
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from ..schemas import ClientInfo
from .base import AdapterAuthError, PlatformAdapter, QuotaDepletedError
from .ratelimit import request_with_retry
from .schema import JobPosting
from .vault import CredentialVault, StateStore

log = logging.getLogger(__name__)

API_BASE = "https://www.freelancer.com/api"
SANDBOX_BASE = "https://www.freelancer-sandbox.com/api"
AUTHORIZE_URL = "https://accounts.freelancer.com/oauth/authorise"

_JOB_TYPE_MAP = {"fixed": "fixed", "hourly": "hourly"}
_EXP_MAP = {1: "entry", 2: "intermediate", 3: "expert"}


class FreelancerAdapter(PlatformAdapter):
    platform = "freelancer"
    rate_per_sec = 2.0  # conservative; Freelancer does not publish exact limits

    def __init__(self, db: Session, client: httpx.AsyncClient | None = None,
                 sandbox: bool = False, monthly_bid_quota: int = 50):
        super().__init__(client)
        self.base = SANDBOX_BASE if sandbox else API_BASE
        self.vault = CredentialVault(db)
        self.state = StateStore(db)
        self.monthly_bid_quota = monthly_bid_quota

    # ---------------- OAuth 2.0 ----------------

    def build_authorize_url(self, client_id: str, redirect_uri: str,
                            advanced_scopes: list[int] | None = None) -> str:
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": "basic",
            "advanced_scopes": " ".join(str(s) for s in (advanced_scopes or [1, 2, 3])),
        }
        return f"{AUTHORIZE_URL}?{urlencode(params)}"

    async def exchange_code(self, client_id: str, client_secret: str,
                            code: str, redirect_uri: str) -> dict:
        resp = await self._request("POST", f"{self.base}/oauth/token", data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        })
        tokens = self._persist_tokens(resp.json(), client_id, client_secret)
        return tokens

    def _persist_tokens(self, payload: dict, client_id: str, client_secret: str) -> dict:
        expires_in = int(payload.get("expires_in", 3600))
        tokens = {
            "client_id": client_id,
            "client_secret": client_secret,
            "access_token": payload["access_token"],
            "refresh_token": payload.get("refresh_token"),
            "expires_at": datetime.fromtimestamp(
                datetime.now(timezone.utc).timestamp() + expires_in - 60, timezone.utc
            ).isoformat(),
        }
        self.vault.store(self.platform, "default", tokens)
        return tokens

    async def _access_token(self) -> str:
        creds = self.vault.load(self.platform, "default")
        if not creds:
            raise AdapterAuthError("freelancer: no credentials in vault")
        expires_at = datetime.fromisoformat(creds["expires_at"])
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at > datetime.now(timezone.utc):
            return creds["access_token"]
        # refresh
        resp = await self._request("POST", f"{self.base}/oauth/token", data={
            "grant_type": "refresh_token",
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "refresh_token": creds["refresh_token"],
        })
        refreshed = self._persist_tokens(resp.json(), creds["client_id"], creds["client_secret"])
        log.info("freelancer: access token refreshed")
        return refreshed["access_token"]

    async def _api(self, method: str, path: str, **kwargs) -> dict:
        token = await self._access_token()
        resp = await self._request(
            method, f"{self.base}{path}",
            headers={"Authorization": f"Bearer {token}"}, **kwargs
        )
        payload = resp.json()
        if payload.get("status") == "error":
            raise AdapterAuthError(f"freelancer API error: {payload.get('message')} ({payload.get('error_code')})")
        return payload.get("result", payload)

    # ---------------- Read endpoints ----------------

    async def search_jobs(self, query: str = "", skill_ids: list[int] | None = None,
                          min_budget: float | None = None, max_budget: float | None = None,
                          job_types: list[str] | None = None, limit: int = 50,
                          offset: int = 0, **_) -> list[JobPosting]:
        params: dict = {"query": query, "limit": limit, "offset": offset,
                        "project_details": "true", "user_details": "true"}
        if skill_ids:
            params["jobs[]"] = skill_ids
        if min_budget is not None:
            params["min_avg_price"] = min_budget
        if max_budget is not None:
            params["max_avg_price"] = max_budget
        if job_types:
            params["project_types[]"] = [t.upper() for t in job_types if t in _JOB_TYPE_MAP]
        result = await self._api("GET", "/projects/0.1/projects/active/", params=params)
        return [self._normalize_project(p) for p in result.get("projects", [])]

    async def get_job_details(self, external_id: str) -> JobPosting:
        result = await self._api("GET", f"/projects/0.1/projects/{external_id}/")
        return self._normalize_project(result)

    async def get_user_info(self, user_id: str) -> dict:
        return await self._api("GET", f"/users/0.1/users/{user_id}/")

    async def get_threads(self, limit: int = 50, offset: int = 0) -> list[dict]:
        result = await self._api("GET", "/projects/0.1/threads/",
                                 params={"limit": limit, "offset": offset})
        return result.get("threads", [])

    # ---------------- Bidding ----------------

    def _quota(self) -> dict:
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        quota = self.state.get(self.platform, "bid_quota", {})
        if quota.get("month") != month:
            quota = {"month": month, "used": 0}
            self.state.set(self.platform, "bid_quota", quota)
        return quota

    def bids_remaining(self) -> int:
        return max(0, self.monthly_bid_quota - self._quota()["used"])

    async def place_bid(self, project_id: int, bidder_id: int, amount: float,
                        period: int, proposal: str,
                        milestone_percentage: int | None = None) -> dict:
        """Place a bid. Raises QuotaDepletedError when the monthly quota is gone.

        NOTE: callers must pass proposals that have been approved by the human
        review queue — this adapter does not bypass that boundary.
        """
        if self.bids_remaining() <= 0:
            raise QuotaDepletedError(
                f"freelancer: monthly bid quota of {self.monthly_bid_quota} depleted; paused"
            )
        data: dict = {
            "project_id": project_id,
            "bidder_id": bidder_id,
            "amount": amount,
            "period": period,
            "description": proposal,
        }
        if milestone_percentage is not None:
            data["milestone_percentage"] = milestone_percentage
        result = await self._api("POST", "/projects/0.1/bids/", data=data)
        quota = self._quota()
        quota["used"] += 1
        self.state.set(self.platform, "bid_quota", quota)
        return result

    async def get_bid_status(self, bid_id: int) -> dict:
        return await self._api("GET", f"/projects/0.1/bids/{bid_id}/")

    # ---------------- Normalization ----------------

    @staticmethod
    def _normalize_project(p: dict) -> JobPosting:
        budget = p.get("budget") or {}
        currency = (p.get("currency") or {}).get("code", "USD")
        ptype = (p.get("type") or "").lower()
        submitted = p.get("time_submitted")
        client: ClientInfo = ClientInfo()
        owner = p.get("owner") or {}
        if owner:
            client = ClientInfo(
                payment_verified=(owner.get("payment_verified")
                                  or (owner.get("status") or {}).get("payment_verified")),
                country=(owner.get("location") or {}).get("country", {}).get("name"),
                rating=(owner.get("reputation") or {}).get("entire_history", {}).get("overall"),
                reviews_count=(owner.get("reputation") or {}).get("entire_history", {}).get("reviews"),
            )
        return JobPosting(
            source_platform="freelancer",
            external_id=str(p.get("id")),
            title=p.get("title", ""),
            description=p.get("description", "") or p.get("preview_description", ""),
            url=p.get("seo_url") and f"https://www.freelancer.com/projects/{p['seo_url']}" or "",
            job_type=_JOB_TYPE_MAP.get(ptype),
            budget_min=budget.get("minimum"),
            budget_max=budget.get("maximum"),
            currency=currency,
            skills=[j.get("name", "") for j in (p.get("jobs") or [])],
            client_info=client,
            proposals_count=p.get("bid_stats", {}).get("bid_count"),
            posted_at=datetime.fromtimestamp(submitted, timezone.utc) if submitted else None,
            raw_data=p,
        )
