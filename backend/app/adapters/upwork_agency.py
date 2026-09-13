"""Upwork agency discovery and permission-dependent browser handoff.

API availability and platform permission must be verified for each deployment.
An agency subscription alone is not evidence of permission for browser automation.
The adapter records reviewed submission requests and tenant-owned audit events;
the worker's external-write gate remains independently controlled.
"""
import logging
import hashlib
import copy
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from ..models import AgencyAuditLog
from ..schemas import ClientInfo
from .base import AdapterAuthError, PlatformAdapter
from .schema import JobPosting
from .vault import CredentialVault, StateStore

log = logging.getLogger(__name__)

GRAPHQL_URL = "https://api.upwork.com/graphql"
AUTHORIZE_URL = "https://www.upwork.com/ab/account-security/oauth2/authorize"
TOKEN_URL = "https://www.upwork.com/api/v3/oauth2/token"

AGENCY_PRINCIPAL = "agency_manager"

_SEARCH_QUERY = """
query SearchJobs($filter: MarketplaceJobPostingsSearchFilter!) {
  marketplaceJobPostingsSearch(marketPlaceJobFilter: $filter) {
    totalCount
    edges {
      node {
        id
        title
        description
        createdDateTime
        ciphertext
        hourlyBudgetMin { rawValue }
        hourlyBudgetMax { rawValue }
        fixedPriceBudget { amount { rawValue displayValue } }
        engagementDuration { label }
        skills { name }
        client {
          totalHires
          totalPostedJobs
          totalSpent { rawValue }
          paymentVerificationStatus
          location { country }
        }
        proposalsTier
      }
    }
  }
}
"""

_PROPOSALS_TIER_MAP = {
    "0 to 4": 4, "5 to 10": 10, "10 to 15": 15,
    "15 to 20": 20, "20 to 50": 50, "50+": 60,
}


class UpworkAgencyAdapter(PlatformAdapter):
    platform = "upwork"
    rate_per_sec = 8.0  # Upwork documents 10 req/s per IP; stay under

    def __init__(self, db: Session, user_id: int, client: httpx.AsyncClient | None = None, *, principal: str | None = None):
        if principal is None:
            from .accounts import default_principal
            principal = default_principal(db, user_id, self.platform, AGENCY_PRINCIPAL)
        super().__init__(client, principal=f"user{user_id}:{principal}")
        self.account_principal = principal
        self.db = db
        self.user_id = user_id
        self.vault = CredentialVault(db, user_id)
        self.state = StateStore(db, user_id)

    # ---------------- audit ----------------

    def _audit(self, action: str, target: str = "", detail: dict | None = None):
        self.db.add(AgencyAuditLog(
            user_id=self.user_id, actor=self.account_principal, action=action, target=target, detail=detail or {}
        ))
        self.db.commit()

    # ---------------- OAuth 2.0 (agency manager account) ----------------

    async def _access_token(self) -> str:
        creds = self.vault.load(self.platform, self.account_principal)
        if not creds:
            raise AdapterAuthError("upwork: no agency_manager credentials in vault")
        if not creds.get("expires_at"):
            return creds["access_token"]
        expires_at = datetime.fromisoformat(creds["expires_at"])
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at > datetime.now(timezone.utc):
            return creds["access_token"]
        if not all(creds.get(k) for k in ("client_id", "client_secret", "refresh_token")):
            raise AdapterAuthError("upwork: expired token requires refresh metadata; re-enroll credentials")
        resp = await self._request("POST", TOKEN_URL, data={
            "grant_type": "refresh_token",
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "refresh_token": creds["refresh_token"],
        })
        payload = resp.json()
        expires_in = int(payload.get("expires_in", 86400))
        creds.update({
            "access_token": payload["access_token"],
            "refresh_token": payload.get("refresh_token", creds["refresh_token"]),
            "expires_at": datetime.fromtimestamp(
                datetime.now(timezone.utc).timestamp() + expires_in - 60, timezone.utc
            ).isoformat(),
        })
        self.vault.store(self.platform, self.account_principal, creds)
        self._audit("oauth.token_refresh")
        log.info("upwork: agency manager access token refreshed")
        return creds["access_token"]

    async def _graphql(self, query: str, variables: dict) -> dict:
        token = await self._access_token()
        resp = await self._request(
            "POST", GRAPHQL_URL,
            headers={"Authorization": f"Bearer {token}"},
            json={"query": query, "variables": variables},
        )
        payload = resp.json()
        if payload.get("errors"):
            raise AdapterAuthError(f"upwork GraphQL error: {payload['errors']}")
        return payload["data"]

    # ---------------- Discovery (official API) ----------------

    async def search_jobs(self, query: str = "", limit: int = 50, **_) -> list[JobPosting]:
        self._consume_daily_action()
        data = await self._graphql(_SEARCH_QUERY, {
            "filter": {"searchExpression": {"andTerms": {"anyOf": query}}, "pagination": {"first": min(limit, 100)}}
        })
        edges = (data.get("marketplaceJobPostingsSearch") or {}).get("edges") or []
        return [self._normalize(e["node"]) for e in edges]

    async def get_job_details(self, external_id: str) -> JobPosting:
        data = await self._graphql(_SEARCH_QUERY, {
            "filter": {"searchExpression": {"andTerms": {"anyOf": external_id}}, "pagination": {"first": 1}}
        })
        edges = (data.get("marketplaceJobPostingsSearch") or {}).get("edges") or []
        if not edges:
            raise AdapterAuthError(f"upwork: job {external_id} not found")
        return self._normalize(edges[0]["node"])

    # ---------------- Agency member management ----------------

    @property
    def _roster_key(self):
        # Historical unscoped rosters require explicit assignment by their owner.
        return "agency_roster:" + hashlib.sha256(self.account_principal.encode()).hexdigest()

    def list_agency_members(self) -> list[dict]:
        return self.state.get(self.platform, self._roster_key, {"members": []})["members"]

    def _lock_roster(self):
        from ..models import User
        owner = self.db.query(User).filter_by(id=self.user_id).populate_existing().with_for_update().one()
        if not owner.is_active:
            raise AdapterAuthError('account owner is inactive')

    def add_agency_member(self, freelancer_username: str) -> list[dict]:
        """Track a roster addition. The actual invitation must be completed in
        Upwork's UI by the agency manager (Upwork exposes no API for this);
        the roster records intent + audit so the hybrid flow stays consistent.
        """
        self._lock_roster()
        roster = copy.deepcopy(self.state.get(self.platform, self._roster_key, {"members": []}))
        if not any(m["username"] == freelancer_username for m in roster["members"]):
            roster["members"].append({
                "username": freelancer_username,
                "status": "invitation_pending",
                "added_at": datetime.now(timezone.utc).isoformat(),
            })
            self.state.set(self.platform, self._roster_key, roster)
        self._audit("agency.member_add", target=freelancer_username)
        return roster["members"]

    def remove_agency_member(self, freelancer_username: str) -> list[dict]:
        self._lock_roster()
        roster = copy.deepcopy(self.state.get(self.platform, self._roster_key, {"members": []}))
        roster["members"] = [m for m in roster["members"] if m["username"] != freelancer_username]
        self.state.set(self.platform, self._roster_key, roster)
        self._audit("agency.member_remove", target=freelancer_username)
        return roster["members"]

    # ---------------- Proposal submission (hybrid handoff) ----------------

    def submit_proposal(self, job_external_id: str, proposal_text: str,
                        on_behalf_of: str, connects_required: int = 0,
                        approved_by: str | None = None, *, persist: bool = True) -> dict:
        """Queue an agency-manager proposal submission for browser execution.

        Hard requirements (enforced here):
          * `on_behalf_of` must be a current agency roster member;
          * `approved_by` must identify the app reviewer. Provider permission
            is a separate deployment requirement.

        Returns the queued submission record; a browser worker with the agency
        manager's authenticated session performs the actual submission and
        calls `complete_submission`.
        """
        if not approved_by:
            raise AdapterAuthError(
                "upwork: proposal submission requires human approval (approved_by) — "
                "human-in-the-loop is mandatory"
            )
        members = {m["username"] for m in self.list_agency_members()}
        if on_behalf_of not in members:
            raise AdapterAuthError(f"upwork: '{on_behalf_of}' is not an agency member")
        self._consume_daily_action()

        queue = self.state.get(self.platform, "pending_submissions", {"items": []})
        record = {
            "job_external_id": job_external_id,
            "proposal_text": proposal_text,
            "on_behalf_of": on_behalf_of,
            "connects_required": connects_required,
            "approved_by": approved_by,
            "status": "pending_browser_execution",
            "queued_at": datetime.now(timezone.utc).isoformat(),
        }
        if not persist:
            return record
        queue["items"].append(record)
        self.state.set(self.platform, "pending_submissions", queue)
        self._audit("proposal.submit_queued", target=job_external_id, detail={
            "on_behalf_of": on_behalf_of, "approved_by": approved_by,
            "connects_required": connects_required,
        })
        return record

    def complete_submission(self, job_external_id: str, success: bool, note: str = ""):
        queue = self.state.get(self.platform, "pending_submissions", {"items": []})
        for item in queue["items"]:
            if item["job_external_id"] == job_external_id and item["status"] == "pending_browser_execution":
                item["status"] = "submitted" if success else "failed"
                item["completed_at"] = datetime.now(timezone.utc).isoformat()
                item["note"] = note
        self.state.set(self.platform, "pending_submissions", queue)
        self._audit("proposal.submit_completed", target=job_external_id,
                    detail={"success": success, "note": note})

    # ---------------- Normalization ----------------

    @staticmethod
    def _normalize(node: dict) -> JobPosting:
        fixed = (node.get("fixedPriceBudget") or {}).get("amount") or {}
        fixed_val = fixed.get("rawValue")
        hmin = (node.get("hourlyBudgetMin") or {}).get("rawValue")
        hmax = (node.get("hourlyBudgetMax") or {}).get("rawValue")
        hourly = hmin is not None or hmax is not None
        client = node.get("client") or {}
        ciphertext = node.get("ciphertext") or node.get("id", "")
        return JobPosting(
            source_platform="upwork",
            external_id=str(node.get("id", ciphertext)),
            title=node.get("title", ""),
            description=node.get("description", ""),
            url=f"https://www.upwork.com/jobs/{ciphertext}" if ciphertext else "",
            job_type="hourly" if hourly else ("fixed" if fixed_val else None),
            budget_min=float(hmin) if hourly and hmin is not None else (float(fixed_val) if fixed_val else None),
            budget_max=float(hmax) if hourly and hmax is not None else (float(fixed_val) if fixed_val else None),
            currency="USD",
            skills=[s.get("name", "") for s in (node.get("skills") or [])],
            client_info=ClientInfo(
                payment_verified=(client.get("paymentVerificationStatus") == "VERIFIED"),
                total_spent=float((client.get("totalSpent") or {}).get("rawValue") or 0),
                country=(client.get("location") or {}).get("country"),
                hire_rate=(round(100 * client["totalHires"] / client["totalPostedJobs"], 1)
                           if client.get("totalPostedJobs") else None),
                past_hires=client.get("totalHires"),
                # search results carry no stable client id; keep it when a
                # richer query (job details) does provide one
                client_id=str(client["id"]) if client.get("id") else None,
            ),
            proposals_count=_PROPOSALS_TIER_MAP.get(node.get("proposalsTier", ""), None),
            posted_at=node.get("createdDateTime"),
            raw_data=node,
        )
