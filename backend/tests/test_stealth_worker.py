"""Tests for the stealth-worker protocol (AD-4): atomic claim, worker-token
auth, windowed circuit-breaker counting, and submission-outcome handoff."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import circuit_breaker
from app.auth import create_access_token, hash_password
from app.database import Base, get_db
from app.main import app
from app.models import Job, ProposalQueueItem, StealthTask, User

WORKER_HEADERS = {"Authorization": "Bearer test-worker-token"}


@pytest.fixture(autouse=True)
def force_offline(monkeypatch):
    monkeypatch.setattr("app.circuit_breaker.cache._r", None)
    circuit_breaker._local.clear()


@pytest.fixture()
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c, TestingSession
    app.dependency_overrides.clear()


def _user(db, email):
    u = User(email=email, password_hash=hash_password("password123"))
    db.add(u)
    db.commit()
    return u


def _task(db, user_id, platform="fiverr", task_type="fetch_buyer_requests",
          status="pending", payload=None, completed_at=None, claimed_by=None):
    t = StealthTask(user_id=user_id, platform=platform, task_type=task_type,
                    payload=payload or {}, status=status,
                    completed_at=completed_at, claimed_by=claimed_by)
    db.add(t)
    db.commit()
    return t


# ---------------- claim ----------------

def test_claim_success_then_conflict(client):
    c, Session = client
    db = Session()
    u = _user(db, "claim@example.com")
    t = _task(db, u.id)

    r = c.post(f"/api/gigs/stealth-tasks/{t.id}/claim",
               json={"worker_id": "w-1"}, headers=WORKER_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "claimed"
    assert body["claimed_by"] == "w-1"
    assert body["payload"] == {}
    assert body["user_id"] == u.id

    # second claim (same or different worker) loses the race
    r2 = c.post(f"/api/gigs/stealth-tasks/{t.id}/claim",
                json={"worker_id": "w-2"}, headers=WORKER_HEADERS)
    assert r2.status_code == 409

    r3 = c.post("/api/gigs/stealth-tasks/99999/claim",
                json={"worker_id": "w-1"}, headers=WORKER_HEADERS)
    assert r3.status_code == 404


def test_claim_requires_worker_token(client):
    c, Session = client
    db = Session()
    u = _user(db, "claimauth@example.com")
    t = _task(db, u.id)
    user_headers = {"Authorization": f"Bearer {create_access_token(u)}"}

    assert c.post(f"/api/gigs/stealth-tasks/{t.id}/claim",
                  json={"worker_id": "w"}).status_code == 401
    assert c.post(f"/api/gigs/stealth-tasks/{t.id}/claim",
                  json={"worker_id": "w"}, headers=user_headers).status_code == 401
    assert c.post(f"/api/gigs/stealth-tasks/{t.id}/claim",
                  json={"worker_id": "w"},
                  headers={"Authorization": "Bearer wrong"}).status_code == 401
    db.refresh(t)
    assert t.status == "pending"


# ---------------- poll ----------------

def test_poll_worker_cross_tenant_and_user_scoped(client):
    c, Session = client
    db = Session()
    u1 = _user(db, "poll1@example.com")
    u2 = _user(db, "poll2@example.com")
    _task(db, u1.id, platform="fiverr", payload={"k": 1})
    _task(db, u2.id, platform="upwork", task_type="submit_upwork_proposal")
    _task(db, u1.id, platform="fiverr", status="done")

    r = c.get("/api/gigs/stealth-tasks?status=pending", headers=WORKER_HEADERS)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2  # both tenants, pending only
    assert {row["user_id"] for row in rows} == {u1.id, u2.id}
    assert all("payload" in row and "task_type" in row for row in rows)

    r = c.get("/api/gigs/stealth-tasks?status=pending&platform=fiverr",
              headers=WORKER_HEADERS)
    assert [row["platform"] for row in r.json()] == ["fiverr"]

    # user JWT path stays tenant-scoped (UI display)
    r = c.get("/api/gigs/stealth-tasks?status=pending",
              headers={"Authorization": f"Bearer {create_access_token(u1)}"})
    assert len(r.json()) == 1
    assert r.json()[0]["user_id"] == u1.id

    # no auth at all → 401
    assert c.get("/api/gigs/stealth-tasks").status_code == 401


# ---------------- complete ----------------

def test_complete_requires_worker_token(client):
    c, Session = client
    db = Session()
    u = _user(db, "completeauth@example.com")
    t = _task(db, u.id, status="claimed")
    user_headers = {"Authorization": f"Bearer {create_access_token(u)}"}
    assert c.post(f"/api/gigs/stealth-tasks/{t.id}/complete",
                  json={"success": True}).status_code == 401
    assert c.post(f"/api/gigs/stealth-tasks/{t.id}/complete",
                  json={"success": True}, headers=user_headers).status_code == 401


def test_complete_transitions(client):
    c, Session = client
    db = Session()
    u = _user(db, "transitions@example.com")

    # pending tasks can no longer be completed directly — claim first
    t1 = _task(db, u.id)
    r = c.post(f"/api/gigs/stealth-tasks/{t1.id}/complete",
               json={"worker_id": "w-1", "success": True, "result": {"requests": []}},
               headers=WORKER_HEADERS)
    assert r.status_code == 409

    # claim → complete by the claiming worker
    r = c.post(f"/api/gigs/stealth-tasks/{t1.id}/claim",
               json={"worker_id": "w-1"}, headers=WORKER_HEADERS)
    assert r.status_code == 200
    r = c.post(f"/api/gigs/stealth-tasks/{t1.id}/complete",
               json={"worker_id": "w-1", "success": True, "result": {"requests": []}},
               headers=WORKER_HEADERS)
    assert r.status_code == 200 and r.json()["status"] == "done"
    db.refresh(t1)
    assert t1.result == {"requests": []}
    assert t1.completed_at is not None

    # claimed → failed
    t2 = _task(db, u.id, status="claimed", claimed_by="w-1")
    r = c.post(f"/api/gigs/stealth-tasks/{t2.id}/complete",
               json={"worker_id": "w-1", "success": False, "result": {"captcha": True}},
               headers=WORKER_HEADERS)
    assert r.status_code == 200 and r.json()["status"] == "failed"

    # terminal states reject further completion
    r = c.post(f"/api/gigs/stealth-tasks/{t1.id}/complete",
               json={"worker_id": "w-1", "success": True}, headers=WORKER_HEADERS)
    assert r.status_code == 409


def test_complete_bound_to_claiming_worker(client):
    c, Session = client
    db = Session()
    u = _user(db, "binding@example.com")
    t = _task(db, u.id, status="claimed", claimed_by="w-1")

    # a different worker (or none) holding the shared token cannot complete it
    r = c.post(f"/api/gigs/stealth-tasks/{t.id}/complete",
               json={"worker_id": "w-2", "success": False},
               headers=WORKER_HEADERS)
    assert r.status_code == 409
    r = c.post(f"/api/gigs/stealth-tasks/{t.id}/complete",
               json={"success": True}, headers=WORKER_HEADERS)
    assert r.status_code == 409
    db.refresh(t)
    assert t.status == "claimed"

    # the claiming worker succeeds
    r = c.post(f"/api/gigs/stealth-tasks/{t.id}/complete",
               json={"worker_id": "w-1", "success": True},
               headers=WORKER_HEADERS)
    assert r.status_code == 200 and r.json()["status"] == "done"

    # and the completion is audit-logged
    from app.models import AuditLog
    row = (db.query(AuditLog)
           .filter(AuditLog.action_type == "stealth_task_completed")
           .one())
    assert row.user_id == u.id and row.platform == "fiverr"
    assert row.detail == {"task_id": t.id, "worker_id": "w-1", "success": True}


def test_windowed_circuit_breaker(client):
    c, Session = client
    db = Session()
    u = _user(db, "circuit@example.com")

    # an old failure outside the 1h window must not count
    _task(db, u.id, status="failed",
          completed_at=datetime.now(timezone.utc) - timedelta(hours=2))

    def fail_one():
        t = _task(db, u.id, status="claimed", claimed_by="w-1")
        r = c.post(f"/api/gigs/stealth-tasks/{t.id}/complete",
                   json={"worker_id": "w-1", "success": False,
                         "result": {"error": "boom"}},
                   headers=WORKER_HEADERS)
        assert r.status_code == 200

    fail_one()
    fail_one()
    assert circuit_breaker.get_state("fiverr", u.id)["state"] == "closed"  # 2 in window < 3
    fail_one()
    state = circuit_breaker.get_state("fiverr", u.id)
    assert state["state"] == "open"
    assert "failures in the last hour" in state["reason"]
    # the trip is per-tenant: the platform-global circuit stays closed
    assert circuit_breaker.get_state("fiverr")["state"] == "closed"


def test_per_tenant_circuit_isolation(client):
    """Tenant A's failures trip A's circuit; tenant B keeps enqueueing."""
    c, Session = client
    db = Session()
    a = _user(db, "tenant-a@example.com")
    b = _user(db, "tenant-b@example.com")

    # 3 in-window failures for tenant A
    for _ in range(3):
        t = _task(db, a.id, status="claimed", claimed_by="w-1")
        r = c.post(f"/api/gigs/stealth-tasks/{t.id}/complete",
                   json={"worker_id": "w-1", "success": False,
                         "result": {"error": "boom"}},
                   headers=WORKER_HEADERS)
        assert r.status_code == 200

    allowed_a, _ = circuit_breaker.check("fiverr", a.id)
    allowed_b, _ = circuit_breaker.check("fiverr", b.id)
    assert not allowed_a  # A is halted
    assert allowed_b      # B is unaffected
    assert circuit_breaker.check("fiverr")[0]  # global scope untouched

    # a manual platform-wide open still blocks every tenant
    circuit_breaker.open_circuit("fiverr", "manual halt")
    try:
        assert not circuit_breaker.check("fiverr", b.id)[0]
        assert not circuit_breaker.check("fiverr")[0]
    finally:
        circuit_breaker.close_circuit("fiverr", "test cleanup")


def test_complete_submission_flips_queue_item(client):
    c, Session = client
    db = Session()
    u = _user(db, "handoff@example.com")
    job = Job(user_id=u.id, external_id="~abc123", platform="upwork",
              title="Job", url="https://www.upwork.com/jobs/~abc123")
    db.add(job)
    db.commit()
    item = ProposalQueueItem(user_id=u.id, job_id=job.id, platform="upwork",
                             proposal_text="hi", status="queued_for_browser")
    db.add(item)
    db.commit()

    payload = {"proposal_queue_item_id": item.id, "job_external_id": "~abc123"}
    t = _task(db, u.id, platform="upwork", task_type="submit_upwork_proposal",
              status="claimed", claimed_by="w-1", payload=payload)
    r = c.post(f"/api/gigs/stealth-tasks/{t.id}/complete",
               json={"worker_id": "w-1", "success": True,
                     "result": {"submitted": True}},
               headers=WORKER_HEADERS)
    assert r.status_code == 200
    db.refresh(item)
    assert item.status == "submitted"

    # failed submission → item failed with the error carried over
    item2 = ProposalQueueItem(user_id=u.id, job_id=job.id, platform="upwork",
                              proposal_text="hi", status="queued_for_browser")
    db.add(item2)
    db.commit()
    t2 = _task(db, u.id, platform="upwork", task_type="submit_upwork_proposal",
               status="claimed", claimed_by="w-1",
               payload={"proposal_queue_item_id": item2.id, "job_external_id": "~def456"})
    r = c.post(f"/api/gigs/stealth-tasks/{t2.id}/complete",
               json={"worker_id": "w-1", "success": False,
                     "result": {"error": "challenge page"}},
               headers=WORKER_HEADERS)
    assert r.status_code == 200
    db.refresh(item2)
    assert item2.status == "failed"
    assert item2.submission_result["error"] == "challenge page"


# ---------------- worker-posted results ----------------

def test_worker_posts_results(client):
    c, Session = client
    db = Session()
    from app.models import Gig
    u = _user(db, "results@example.com")
    gig = Gig(user_id=u.id, platform="fiverr", title="g", url="https://x/g")
    db.add(gig)
    db.commit()

    # metrics: tenancy resolves via the gig, not the token
    r = c.post("/api/gigs/metrics",
               json={"gig_id": gig.id, "impressions": 10, "clicks": 2},
               headers=WORKER_HEADERS)
    assert r.status_code == 201, r.text

    # competitor snapshots + buyer requests need explicit user_id from workers
    r = c.post("/api/gigs/competitors",
               json={"platform": "fiverr", "category": "logo", "gigs": []},
               headers=WORKER_HEADERS)
    assert r.status_code == 422
    r = c.post("/api/gigs/competitors",
               json={"user_id": u.id, "platform": "fiverr", "category": "logo",
                     "gigs": [{"title": "x", "price": 50}]},
               headers=WORKER_HEADERS)
    assert r.status_code == 201, r.text

    r = c.post("/api/gigs/buyer-requests/process",
               json={"requests": []}, headers=WORKER_HEADERS)
    assert r.status_code == 422
    r = c.post("/api/gigs/buyer-requests/process",
               json={"user_id": u.id, "requests": []}, headers=WORKER_HEADERS)
    assert r.status_code == 200, r.text
