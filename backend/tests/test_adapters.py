"""Adapter tests — all HTTP mocked via httpx.MockTransport, no network."""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.adapters.base import AdapterAuthError, QuotaDepletedError
from app.adapters.freelancer import FreelancerAdapter
from app.adapters.linkedin import LinkedInJobsAdapter
from app.adapters.upwork_agency import UpworkAgencyAdapter
from app.adapters.vault import CredentialVault
from app.auth import hash_password
from app.database import Base
from app.models import AgencyAuditLog, User


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def user(db):
    u = User(email="adapter-test@example.com",
             password_hash=hash_password("password123"), display_name="Adapter Test")
    db.add(u)
    db.commit()
    return u


def _mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)


def _seed_freelancer_creds(db, user_id, expired=False):
    vault = CredentialVault(db, user_id)
    expires = datetime.now(timezone.utc) + (timedelta(seconds=-10) if expired else timedelta(hours=1))
    vault.store("freelancer", "default", {
        "client_id": "cid", "client_secret": "sec",
        "access_token": "OLD_TOKEN", "refresh_token": "REFRESH",
        "expires_at": expires.isoformat(),
    })


FL_PROJECT = {
    "id": 12345, "title": "Build a React app", "description": "Need react + typescript",
    "seo_url": "build-react-app-12345", "type": "FIXED", "time_submitted": 1755000000,
    "budget": {"minimum": 500, "maximum": 1000},
    "currency": {"code": "USD"},
    "jobs": [{"name": "React"}, {"name": "TypeScript"}],
    "bid_stats": {"bid_count": 7},
    "owner": {"payment_verified": True, "location": {"country": {"name": "United States"}},
              "reputation": {"entire_history": {"overall": 4.8, "reviews": 30}}},
}


# ---------------- Vault ----------------

def test_vault_roundtrip(db, user):
    vault = CredentialVault(db, user.id)
    vault.store("freelancer", "default", {"api_key": "secret123"})
    assert vault.load("freelancer", "default") == {"api_key": "secret123"}
    # principals are isolated
    assert vault.load("freelancer", "agency_manager") is None
    # tenants are isolated
    other = User(email="other@example.com", password_hash="x")
    db.add(other)
    db.commit()
    assert CredentialVault(db, other.id).load("freelancer", "default") is None
    # raw DB blob is not plaintext
    from app.models import AdapterCredential
    row = db.query(AdapterCredential).first()
    assert "secret123" not in row.blob
    vault.delete("freelancer", "default")
    assert vault.load("freelancer", "default") is None


# ---------------- Freelancer ----------------

@pytest.mark.asyncio
async def test_freelancer_search_normalization(db, user):
    _seed_freelancer_creds(db, user.id)

    def handler(request: httpx.Request):
        assert request.headers["Authorization"] == "Bearer OLD_TOKEN"
        assert request.url.path == "/api/projects/0.1/projects/active/"
        return httpx.Response(200, json={"status": "success", "result": {"projects": [FL_PROJECT]}})

    async with _mock_client(handler) as client:
        adapter = FreelancerAdapter(db, user.id, client=client)
        jobs = await adapter.search_jobs("react")
    assert len(jobs) == 1
    j = jobs[0]
    assert j.source_platform == "freelancer" and j.external_id == "12345"
    assert j.job_type == "fixed" and j.budget_min == 500 and j.currency == "USD"
    assert j.skills == ["React", "TypeScript"] and j.proposals_count == 7
    assert j.client_info.payment_verified is True
    assert j.raw_data["id"] == 12345


@pytest.mark.asyncio
async def test_freelancer_token_refresh(db, user):
    _seed_freelancer_creds(db, user.id, expired=True)
    calls = []

    def handler(request: httpx.Request):
        calls.append(request.url.path)
        if request.url.path == "/api/oauth/token":
            return httpx.Response(200, json={
                "access_token": "NEW_TOKEN", "refresh_token": "NEW_REFRESH", "expires_in": 3600,
            })
        assert request.headers["Authorization"] == "Bearer NEW_TOKEN"
        return httpx.Response(200, json={"status": "success", "result": {"projects": []}})

    async with _mock_client(handler) as client:
        adapter = FreelancerAdapter(db, user.id, client=client)
        await adapter.search_jobs("react")
    assert "/api/oauth/token" in calls
    assert CredentialVault(db, user.id).load("freelancer", "default")["access_token"] == "NEW_TOKEN"


@pytest.mark.asyncio
async def test_freelancer_bid_quota(db, user):
    _seed_freelancer_creds(db, user.id)
    bids_placed = []

    def handler(request: httpx.Request):
        if request.url.path == "/api/projects/0.1/bids/" and request.method == "POST":
            bids_placed.append(request.content)
            return httpx.Response(200, json={"status": "success", "result": {"id": 999}})
        return httpx.Response(200, json={"status": "success", "result": {}})

    async with _mock_client(handler) as client:
        adapter = FreelancerAdapter(db, user.id, client=client, monthly_bid_quota=2)
        await adapter.place_bid(12345, 777, 500, 7, "proposal text")
        await adapter.place_bid(12346, 777, 600, 7, "proposal text 2")
        assert adapter.bids_remaining() == 0
        with pytest.raises(QuotaDepletedError):
            await adapter.place_bid(12347, 777, 700, 7, "should not go out")
    assert len(bids_placed) == 2  # third bid never hit the API
    body = dict(part.split(b"=") for part in bids_placed[0].split(b"&"))
    assert body[b"project_id"] == b"12345" and body[b"amount"] == b"500"


@pytest.mark.asyncio
async def test_freelancer_api_error_envelope(db, user):
    _seed_freelancer_creds(db, user.id)

    def handler(request: httpx.Request):
        return httpx.Response(200, json={
            "status": "error", "message": "invalid project", "error_code": "BAD_PROJECT",
        })

    async with _mock_client(handler) as client:
        adapter = FreelancerAdapter(db, user.id, client=client)
        with pytest.raises(AdapterAuthError, match="invalid project"):
            await adapter.get_job_details("999")


# ---------------- Upwork Agency ----------------

UW_NODE = {
    "id": "abc123", "ciphertext": "~01abc", "title": "Full-stack dev needed",
    "description": "Django + React work", "createdDateTime": "2026-08-08T10:00:00.000Z",
    "fixedPriceBudget": {"amount": {"rawValue": "2500.0"}},
    "hourlyBudgetMin": None, "hourlyBudgetMax": None,
    "skills": [{"name": "Django"}, {"name": "React"}],
    "client": {"totalHires": 20, "totalPostedJobs": 25,
               "totalSpent": {"rawValue": "30000"}, "paymentVerificationStatus": "VERIFIED",
               "location": {"country": "United States"}},
    "proposalsTier": "5 to 10",
}


def _seed_upwork_creds(db, user_id):
    CredentialVault(db, user_id).store("upwork", "agency_manager", {
        "client_id": "cid", "client_secret": "sec",
        "access_token": "UW_TOKEN", "refresh_token": "UW_REFRESH",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    })


@pytest.mark.asyncio
async def test_upwork_search_normalization(db, user):
    _seed_upwork_creds(db, user.id)

    def handler(request: httpx.Request):
        assert request.headers["Authorization"] == "Bearer UW_TOKEN"
        body = json.loads(request.content)
        assert "marketplaceJobPostingsSearch" in body["query"]
        return httpx.Response(200, json={"data": {
            "marketplaceJobPostingsSearch": {"totalCount": 1, "edges": [{"node": UW_NODE}]},
        }})

    async with _mock_client(handler) as client:
        adapter = UpworkAgencyAdapter(db, user.id, client=client)
        jobs = await adapter.search_jobs("django")
    assert len(jobs) == 1
    j = jobs[0]
    assert j.source_platform == "upwork" and j.job_type == "fixed"
    assert j.budget_min == 2500.0 and j.url.endswith("~01abc")
    assert j.client_info.payment_verified is True and j.client_info.hire_rate == 80.0
    assert j.proposals_count == 10


def test_upwork_proposal_requires_human_approval(db, user):
    adapter = UpworkAgencyAdapter(db, user.id)
    adapter.add_agency_member("freelancer_jane")
    with pytest.raises(AdapterAuthError, match="human approval"):
        adapter.submit_proposal("job1", "text", on_behalf_of="freelancer_jane")


def test_upwork_proposal_requires_roster_membership(db, user):
    adapter = UpworkAgencyAdapter(db, user.id)
    with pytest.raises(AdapterAuthError, match="not an agency member"):
        adapter.submit_proposal("job1", "text", on_behalf_of="stranger", approved_by="operator")


def test_upwork_proposal_queue_and_audit(db, user):
    adapter = UpworkAgencyAdapter(db, user.id)
    adapter.add_agency_member("freelancer_jane")
    rec = adapter.submit_proposal("job1", "Hi, I can help...", on_behalf_of="freelancer_jane",
                                  connects_required=6, approved_by="operator_bob")
    assert rec["status"] == "pending_browser_execution"
    adapter.complete_submission("job1", success=True, note="submitted via BM session")

    logs = db.query(AgencyAuditLog).order_by(AgencyAuditLog.id).all()
    actions = [l.action for l in logs]
    assert actions == ["agency.member_add", "proposal.submit_queued", "proposal.submit_completed"]
    assert all(l.actor == "agency_manager" for l in logs)
    queue = adapter.state.get("upwork", "pending_submissions")
    assert queue["items"][0]["status"] == "submitted"


def test_upwork_credentials_isolated_per_principal(db, user):
    vault = CredentialVault(db, user.id)
    vault.store("upwork", "agency_manager", {"access_token": "AGENCY"})
    vault.store("upwork", "default", {"access_token": "FREELANCER"})
    assert vault.load("upwork", "agency_manager")["access_token"] == "AGENCY"
    assert vault.load("upwork", "default")["access_token"] == "FREELANCER"


# ---------------- LinkedIn ----------------

@pytest.mark.asyncio
async def test_linkedin_theirstack_normalization(db, monkeypatch):
    monkeypatch.setenv("THEIRSTACK_API_KEY", "TS_KEY")

    def handler(request: httpx.Request):
        assert request.headers["Authorization"] == "Bearer TS_KEY"
        body = json.loads(request.content)
        assert body["remote_only"] is True
        return httpx.Response(200, json={"data": [{
            "id": 555, "job_title": "Contract Python Developer",
            "description": "6-month contract, Django + FastAPI",
            "url": "https://linkedin.com/jobs/view/555",
            "date_posted": "2026-08-07",
            "remote": True,
            "min_annual_salary_usd": 90000, "max_annual_salary_usd": 120000,
            "employment_statuses": ["Contract"],
            "technology_slugs": ["python", "django"],
            "company_object": {"country": "US"},
        }]})

    async with _mock_client(handler) as client:
        adapter = LinkedInJobsAdapter(db, provider="theirstack", client=client)
        jobs = await adapter.search_jobs("python", remote_only=True)
    assert len(jobs) == 1
    j = jobs[0]
    assert j.source_platform == "linkedin" and j.external_id == "555"
    assert j.job_type == "hourly" and j.work_arrangement == "remote"
    assert j.budget_min == 90000 and j.skills == ["python", "django"]
    assert j.raw_data["id"] == 555


@pytest.mark.asyncio
async def test_linkedin_missing_key_raises(db, monkeypatch):
    monkeypatch.delenv("THEIRSTACK_API_KEY", raising=False)
    adapter = LinkedInJobsAdapter(db, provider="theirstack", client=_mock_client(lambda r: httpx.Response(200)))
    with pytest.raises(AdapterAuthError, match="missing API key"):
        await adapter.search_jobs("python")
