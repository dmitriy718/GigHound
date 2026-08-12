"""LinkedIn job data adapter (READ-ONLY).

LinkedIn exposes no public job-search or candidate-side application API, so
discovery runs through licensed third-party data providers:

  * Bright Data — LinkedIn jobs dataset API
      POST https://api.brightdata.com/datasets/v3/trigger?dataset_id=gd_lpfll7v5hcqtkxl6l
      then poll /snapshot/{id}; or use the synchronous scraper endpoint.
  * TheirStack — job search API
      POST https://api.theirstack.com/v1/jobs/search

Set provider="brightdata" (env BRIGHTDATA_API_KEY) or provider="theirstack"
(env THEIRSTACK_API_KEY). Credentials may also be kept in the vault under
principal "default" as {"api_key": "..."}.

This adapter NEVER automates LinkedIn accounts — it fetches publicly listed
job data only. Application submission is out of scope by design.
"""
import logging
import os
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from ..schemas import ClientInfo
from .base import AdapterAuthError, AdapterError, PlatformAdapter
from .schema import JobPosting
from .vault import CredentialVault

log = logging.getLogger(__name__)

BRIGHTDATA_TRIGGER = "https://api.brightdata.com/datasets/v3/trigger"
BRIGHTDATA_SNAPSHOT = "https://api.brightdata.com/datasets/v3/snapshot"
BRIGHTDATA_DATASET = "gd_lpfll7v5hcqtkxl6l"  # LinkedIn job listings dataset
THEIRSTACK_SEARCH = "https://api.theirstack.com/v1/jobs/search"


class LinkedInJobsAdapter(PlatformAdapter):
    platform = "linkedin"
    rate_per_sec = 1.0

    def __init__(self, db: Session | None = None, provider: str = "theirstack",
                 client: httpx.AsyncClient | None = None):
        super().__init__(client)
        self.provider = provider
        self.vault = CredentialVault(db) if db is not None else None

    def _api_key(self) -> str:
        env_var = "BRIGHTDATA_API_KEY" if self.provider == "brightdata" else "THEIRSTACK_API_KEY"
        key = os.getenv(env_var)
        if not key and self.vault:
            creds = self.vault.load(self.platform, "default")
            key = (creds or {}).get("api_key")
        if not key:
            raise AdapterAuthError(f"linkedin: missing API key (set {env_var} or vault credentials)")
        return key

    async def search_jobs(self, query: str = "", location: str = "",
                          remote_only: bool = True, limit: int = 25, **_) -> list[JobPosting]:
        if self.provider == "brightdata":
            return await self._search_brightdata(query, location, remote_only, limit)
        return await self._search_theirstack(query, location, remote_only, limit)

    async def get_job_details(self, external_id: str) -> JobPosting:
        raise AdapterError(
            "linkedin: detail fetch by id is not supported by third-party providers; "
            "use search results (full payload kept in raw_data)"
        )

    # ---------------- TheirStack ----------------

    async def _search_theirstack(self, query, location, remote_only, limit) -> list[JobPosting]:
        body: dict = {
            "limit": min(limit, 100),
            "job_title_or": [query] if query else [],
            "posted_at_max_age_days": 7,
        }
        if remote_only:
            body["remote_only"] = True
        if location:
            body["job_location_pattern_or"] = [location]
        resp = await self._request(
            "POST", THEIRSTACK_SEARCH,
            headers={"Authorization": f"Bearer {self._api_key()}"},
            json=body,
        )
        jobs = resp.json().get("data", [])
        return [self._normalize_theirstack(j) for j in jobs]

    @staticmethod
    def _normalize_theirstack(j: dict) -> JobPosting:
        posted = j.get("date_posted")
        remote = j.get("remote", False)
        return JobPosting(
            source_platform="linkedin",
            external_id=str(j.get("id") or j.get("url", "")),
            title=j.get("job_title", ""),
            description=j.get("description", ""),
            url=j.get("url", ""),
            job_type="hourly" if j.get("employment_statuses") and "Contract" in j["employment_statuses"] else None,
            budget_min=j.get("min_annual_salary_usd"),
            budget_max=j.get("max_annual_salary_usd"),
            currency="USD",
            skills=j.get("technology_slugs") or [],
            client_info=ClientInfo(country=(j.get("company_object") or {}).get("country")),
            work_arrangement="remote" if remote else ("hybrid" if j.get("hybrid") else "onsite"),
            posted_at=_parse_date(posted),
            raw_data=j,
        )

    # ---------------- Bright Data ----------------

    async def _search_brightdata(self, query, location, remote_only, limit) -> list[JobPosting]:
        key = self._api_key()
        headers = {"Authorization": f"Bearer {key}"}
        loc = location or ("Worldwide" if remote_only else "")
        trigger_body = [{
            "keyword": query,
            "location": loc,
            "country": "",
            "time_range": "Past week",
            "job_type": "",
            "experience_level": "",
            "remote": "On-site/Remote" if remote_only else "",
            "company": "",
        }]
        resp = await self._request(
            "POST", BRIGHTDATA_TRIGGER,
            params={"dataset_id": BRIGHTDATA_DATASET, "include_errors": "true"},
            headers=headers, json=trigger_body,
        )
        snapshot_id = resp.json().get("snapshot_id")
        if not snapshot_id:
            raise AdapterError(f"brightdata: no snapshot_id in trigger response: {resp.text[:200]}")

        # Poll the snapshot until ready (max ~2 min)
        import asyncio

        for _ in range(24):
            await asyncio.sleep(5)
            snap = await self._request(
                "GET", f"{BRIGHTDATA_SNAPSHOT}/{snapshot_id}",
                params={"format": "json"}, headers=headers,
            )
            if snap.status_code == 200:
                data = snap.json()
                if isinstance(data, list):
                    return [self._normalize_brightdata(j) for j in data[:limit]]
            # 202 → still running
        raise AdapterError("brightdata: snapshot did not complete within 2 minutes")

    @staticmethod
    def _normalize_brightdata(j: dict) -> JobPosting:
        return JobPosting(
            source_platform="linkedin",
            external_id=str(j.get("job_posting_id") or j.get("url", "")),
            title=j.get("job_title", ""),
            description=j.get("job_summary", "") or j.get("job_description_formatted", ""),
            url=j.get("url", ""),
            skills=j.get("job_industries") if isinstance(j.get("job_industries"), list) else [],
            client_info=ClientInfo(country=j.get("job_location", "").split(",")[-1].strip() or None),
            work_arrangement="remote" if "remote" in (j.get("job_location", "").lower()) else None,
            posted_at=_parse_date(j.get("job_posted_date")),
            raw_data=j,
        )


def _parse_date(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
