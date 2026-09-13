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
from app.models import Job, PlatformAccount, ProposalQueueItem, StealthTask, User

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
    if status == "pending" and not db.query(PlatformAccount).filter_by(user_id=user_id, platform=platform).first():
        db.add(PlatformAccount(user_id=user_id, platform=platform, label="Test browser account", mode="stealth", enabled=True))
    t = StealthTask(user_id=user_id, platform=platform, task_type=task_type,
                    payload=payload or {}, status=status,
                    completed_at=completed_at, claimed_by=claimed_by,
                    claimed_at=datetime.now(timezone.utc) if status == "claimed" else None,
                    claim_token="test-claim" if status == "claimed" else None)
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
    assert body["payload"]["account_id"] > 0
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
               json={"worker_id": "w-1", "claim_token": "test-claim", "success": True, "result": {"requests": []}},
               headers=WORKER_HEADERS)
    assert r.status_code == 409

    # claim → complete by the claiming worker
    r = c.post(f"/api/gigs/stealth-tasks/{t1.id}/claim",
               json={"worker_id": "w-1"}, headers=WORKER_HEADERS)
    assert r.status_code == 200
    token = r.json()["claim_token"]
    r = c.post(f"/api/gigs/stealth-tasks/{t1.id}/complete",
               json={"worker_id": "w-1", "claim_token": token, "success": True, "result": {"requests": []}},
               headers=WORKER_HEADERS)
    assert r.status_code == 200 and r.json()["status"] == "done"
    db.refresh(t1)
    assert t1.result == {"requests": []}
    assert t1.completed_at is not None

    # claimed → failed
    t2 = _task(db, u.id, status="claimed", claimed_by="w-1")
    r = c.post(f"/api/gigs/stealth-tasks/{t2.id}/complete",
               json={"worker_id": "w-1", "claim_token": "test-claim", "success": False, "result": {"captcha": True}},
               headers=WORKER_HEADERS)
    assert r.status_code == 200 and r.json()["status"] == "failed"

    # terminal states reject further completion
    r = c.post(f"/api/gigs/stealth-tasks/{t1.id}/complete",
               json={"worker_id": "w-1", "claim_token": "test-claim", "success": True}, headers=WORKER_HEADERS)
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
               json={"worker_id": "w-1", "claim_token": "test-claim", "success": True},
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
                   json={"worker_id": "w-1", "claim_token": "test-claim", "success": False,
                         "result": {"error": "boom", "submitted": False}},
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
                   json={"worker_id": "w-1", "claim_token": "test-claim", "success": False,
                         "result": {"error": "boom", "submitted": False}},
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
               json={"worker_id": "w-1", "claim_token": "test-claim", "success": True,
                     "result": {"submitted": True}},
               headers=WORKER_HEADERS)
    assert r.status_code == 200
    db.refresh(item)
    assert item.status == "submitted"

    # failed submission → item failed with the error carried over
    # (own job: one live generated proposal per job — partial unique index)
    job2 = Job(user_id=u.id, external_id="~def456", platform="upwork",
               title="Job 2", url="https://www.upwork.com/jobs/~def456")
    db.add(job2)
    db.commit()
    item2 = ProposalQueueItem(user_id=u.id, job_id=job2.id, platform="upwork",
                              proposal_text="hi", status="queued_for_browser")
    db.add(item2)
    db.commit()
    t2 = _task(db, u.id, platform="upwork", task_type="submit_upwork_proposal",
               status="claimed", claimed_by="w-1",
               payload={"proposal_queue_item_id": item2.id, "job_external_id": "~def456"})
    r = c.post(f"/api/gigs/stealth-tasks/{t2.id}/complete",
               json={"worker_id": "w-1", "claim_token": "test-claim", "success": False,
                     "result": {"error": "challenge page"}},
               headers=WORKER_HEADERS)
    assert r.status_code == 200
    db.refresh(item2)
    assert item2.status == "submitted_unverified"
    assert item2.submission_result["error"] == "challenge page"


# ---------------- worker-posted results ----------------

def test_worker_posts_results(client):
    c, Session = client
    db = Session()
    from app.models import Gig
    u = _user(db, "results@example.com")
    db.add(PlatformAccount(user_id=u.id,platform="fiverr",label="account",mode="stealth"))
    gig = Gig(user_id=u.id, platform="fiverr", title="g", url="https://www.fiverr.com/g")
    db.add(gig);db.commit()
    def claimed(kind,payload):
        t = _task(db,u.id,task_type=kind,payload=payload,status="claimed",claimed_by="w-1")
        return {"task_id":t.id,"worker_id":"w-1","claim_token":"test-claim"}
    metric_claim=claimed("scrape_gig_metrics",{"gigs":[{"id":gig.id,"url":gig.url}]})
    assert c.post("/api/gigs/metrics",json={"gig_id":gig.id},headers=WORKER_HEADERS).status_code==409
    for _ in range(2):
        r=c.post("/api/gigs/metrics",json={**metric_claim,"gig_id":gig.id,"impressions":10,"clicks":2},headers=WORKER_HEADERS)
        assert r.status_code==201,r.text
        assert r.json()["orders"] is None
    from app.models import GigMetric
    assert db.query(GigMetric).filter_by(gig_id=gig.id).count()==1
    competitor_claim=claimed("scrape_competitors",{"category":"logo"})
    r=c.post("/api/gigs/competitors",json={**competitor_claim,"user_id":u.id,"platform":"fiverr","category":"logo","gigs":[]},headers=WORKER_HEADERS)
    assert r.status_code==201,r.text
    requests_claim=claimed("fetch_buyer_requests",{})
    assert c.post("/api/gigs/buyer-requests/process",json={"user_id":u.id,"requests":[]},headers=WORKER_HEADERS).status_code==409
    r=c.post("/api/gigs/buyer-requests/process",json={**requests_claim,"user_id":u.id,"requests":[]},headers=WORKER_HEADERS)
    assert r.status_code==200,r.text


# ---------------- submission-outcome verdicts (P2-2) ----------------

def _submission_item(db, u, job):
    item = ProposalQueueItem(user_id=u.id, job_id=job.id, platform="upwork",
                             proposal_text="hi", status="queued_for_browser")
    db.add(item)
    db.commit()
    return item


def _complete(c, db, u, item, result, success=True):
    t = _task(db, u.id, platform="upwork", task_type="submit_upwork_proposal",
              status="claimed", claimed_by="w-1",
              payload={"proposal_queue_item_id": item.id,
                       "job_external_id": "~abc123"})
    r = c.post(f"/api/gigs/stealth-tasks/{t.id}/complete",
               json={"worker_id": "w-1", "claim_token": "test-claim", "success": success, "result": result},
               headers=WORKER_HEADERS)
    assert r.status_code == 200, r.text
    db.refresh(item)
    return t


def test_confirmed_rejection_flips_item_to_failed_with_reason(client):
    c, Session = client
    db = Session()
    u = _user(db, "verdict-reject@example.com")
    job = Job(user_id=u.id, external_id="~abc123", platform="upwork",
              title="Job", url="https://www.upwork.com/jobs/~abc123")
    db.add(job)
    db.commit()
    # the task "succeeded" (no crash) but the platform rejected the submit —
    # the explicit submitted=False verdict must win over the success flag
    item = _submission_item(db, u, job)
    _complete(c, db, u, item,
              {"submitted": False,
               "reason": "platform rejected the submit (matched 'insufficient connects')"})
    assert item.status == "failed"
    assert "insufficient connects" in item.submission_result["error"]


def test_unverified_submit_flips_item_to_submitted_unverified(client):
    c, Session = client
    db = Session()
    u = _user(db, "verdict-unverified@example.com")
    job = Job(user_id=u.id, external_id="~abc123", platform="upwork",
              title="Job", url="https://www.upwork.com/jobs/~abc123")
    db.add(job)
    db.commit()
    item = _submission_item(db, u, job)
    _complete(c, db, u, item,
              {"submitted": None, "state": "submitted_unverified",
               "reason": "no success/failure marker matched",
               "screenshots": ["/shots/a.png"]})
    assert item.status == "submitted_unverified"
    assert item.submission_result["state"] == "submitted_unverified"
    assert item.submission_result["screenshots"] == ["/shots/a.png"]


def test_manual_assist_gate_no_longer_flips_to_submitted(client):
    c, Session = client
    db = Session()
    u = _user(db, "verdict-manual@example.com")
    job = Job(user_id=u.id, external_id="~abc123", platform="upwork",
              title="Job", url="https://www.upwork.com/jobs/~abc123")
    db.add(job)
    db.commit()
    # manual-assist (WORKER_ALLOW_SUBMIT off): filled only, NOT submitted —
    # must map to failed so a human re-reviews instead of lying "submitted"
    item = _submission_item(db, u, job)
    _complete(c, db, u, item,
              {"manual_assist": True, "filled": True, "submitted": False,
               "note": "form filled only — a human must click the final submit"})
    assert item.status == "failed"
    assert "human must click" in item.submission_result["error"]


def test_submission_outcome_broadcasts_status_change(client, monkeypatch):
    c, Session = client
    db = Session()
    u = _user(db, "verdict-ws@example.com")
    job = Job(user_id=u.id, external_id="~abc123", platform="upwork",
              title="Job", url="https://www.upwork.com/jobs/~abc123")
    db.add(job)
    db.commit()

    sent = []

    async def _capture(user_id, message):
        sent.append((user_id, message))

    monkeypatch.setattr("app.routers.gigs.alerts.broadcast", _capture)

    item = _submission_item(db, u, job)
    _complete(c, db, u, item, {"submitted": True})
    assert item.status == "submitted"
    assert sent == [(u.id, {"type": "proposal_status_changed",
                            "proposal_id": item.id, "status": "submitted"})]

    # a non-submission task (no proposal_queue_item_id) broadcasts nothing
    t = _task(db, u.id, status="claimed", claimed_by="w-1")
    r = c.post(f"/api/gigs/stealth-tasks/{t.id}/complete",
               json={"worker_id": "w-1", "claim_token": "test-claim", "success": True, "result": {}},
               headers=WORKER_HEADERS)
    assert r.status_code == 200
    assert len(sent) == 1

    # an item no longer in queued_for_browser is not re-flipped/re-broadcast
    _complete(c, db, u, item, {"submitted": True})
    assert len(sent) == 1


# ---------------- session-expiry surfacing (P2-3) ----------------

def test_session_expired_audited_and_account_flagged(client):
    c, Session = client
    from app.models import AuditLog, PlatformAccount
    db = Session()
    u = _user(db, "session-exp@example.com")
    account = PlatformAccount(user_id=u.id, platform="fiverr", label="main",
                              settings={"username": "seller1"})
    db.add(account)
    db.commit()

    t = _task(db, u.id, status="claimed", claimed_by="w-1")
    r = c.post(f"/api/gigs/stealth-tasks/{t.id}/complete",
               json={"worker_id": "w-1", "claim_token": "test-claim", "success": False,
                     "result": {"session_expired": True, "platform": "fiverr"}},
               headers=WORKER_HEADERS)
    assert r.status_code == 200
    assert r.json()["status"] == "failed"  # a real failure: feeds the breaker

    row = (db.query(AuditLog)
           .filter(AuditLog.action_type == "session_expired")
           .one())
    assert row.user_id == u.id and row.platform == "fiverr"
    assert row.detail["task_id"] == t.id

    db.refresh(account)
    assert account.settings["needs_reenrollment"] is True
    assert account.settings["username"] == "seller1"  # existing knobs kept


def test_session_expired_counts_toward_breaker(client):
    c, Session = client
    db = Session()
    u = _user(db, "session-breaker@example.com")
    for _ in range(3):
        t = _task(db, u.id, status="claimed", claimed_by="w-1")
        r = c.post(f"/api/gigs/stealth-tasks/{t.id}/complete",
                   json={"worker_id": "w-1", "claim_token": "test-claim", "success": False,
                         "result": {"session_expired": True,
                                    "platform": "fiverr"}},
                   headers=WORKER_HEADERS)
        assert r.status_code == 200
    assert circuit_breaker.get_state("fiverr", u.id)["state"] == "open"


def test_reclaimed_task_rejects_previous_token_even_for_same_worker(client):
    c, Session = client
    with Session() as db:
        user = _user(db, 'fencing@example.test')
        task = _task(db, user.id)
        first = c.post(f'/api/gigs/stealth-tasks/{task.id}/claim', json={'worker_id': 'w-1'}, headers=WORKER_HEADERS).json()
        db.refresh(task)
        task.status = 'pending'; task.claimed_by = None; task.claimed_at = None
        db.commit()
        second = c.post(f'/api/gigs/stealth-tasks/{task.id}/claim', json={'worker_id': 'w-1'}, headers=WORKER_HEADERS).json()
        assert first['claim_token'] != second['claim_token']
        body = {'worker_id': 'w-1', 'claim_token': first['claim_token'], 'success': True, 'result': {}}
        assert c.post(f'/api/gigs/stealth-tasks/{task.id}/complete', json=body, headers=WORKER_HEADERS).status_code == 409
        body['claim_token'] = second['claim_token']
        assert c.post(f'/api/gigs/stealth-tasks/{task.id}/complete', json=body, headers=WORKER_HEADERS).status_code == 200
        assert all('claim_token' not in row for row in c.get('/api/gigs/stealth-tasks?status=done', headers=WORKER_HEADERS).json())


@pytest.mark.parametrize('change', ['disable', 'delete', 'expire'])
def test_authorization_rechecks_account_and_claim(client, change):
    c, Session = client
    with Session() as db:
        user = _user(db, f'{change}@example.test')
        task = _task(db, user.id)
        claim = c.post(f'/api/gigs/stealth-tasks/{task.id}/claim', json={'worker_id': 'w-1'}, headers=WORKER_HEADERS).json()
        body = {'worker_id': 'w-1', 'claim_token': claim['claim_token']}
        assert c.post(f'/api/gigs/stealth-tasks/{task.id}/authorize', json=body, headers=WORKER_HEADERS).status_code == 200
        account = db.query(PlatformAccount).filter_by(user_id=user.id).one()
        if change == 'disable': account.enabled = False
        elif change == 'delete': db.delete(account)
        else:
            db.refresh(task)
            task.claimed_at = datetime.now(timezone.utc) - timedelta(minutes=16)
        db.commit()
        assert c.post(f'/api/gigs/stealth-tasks/{task.id}/authorize', json=body, headers=WORKER_HEADERS).status_code == 409


def test_claim_requires_a_browser_enabled_account(client):
    c, Session = client
    with Session() as db:
        user = _user(db, 'unenrolled@example.test')
        task = StealthTask(user_id=user.id, platform='upwork', task_type='scrape_proposal_status', status='pending')
        db.add(task); db.commit()
        assert c.post(f'/api/gigs/stealth-tasks/{task.id}/claim', json={'worker_id': 'w-1'}, headers=WORKER_HEADERS).status_code == 409
