"""Phase 5 tests: fiverr buyer-request offer dispatch on approve (P5-1),
the mark-submitted manual transition, the honest 400 for non-automated
platforms, and the platform registry single-source-of-truth (P5-2).
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import circuit_breaker
from app.auth import hash_password  # noqa: F401  (parity with other suites)
from app.database import Base, get_db
from app.main import app
from app.models import (AuditLog, Job, PlatformAccount, ProposalQueueItem,
                        StealthTask, User)
from app.platforms import (ALL_PLATFORMS, BROWSER_SYNC_PLATFORMS,
                           DISCOVERY_PLATFORMS, OAUTH_PLATFORMS,
                           STEALTH_CREDENTIAL_PLATFORMS, WORKER_PLATFORMS)

# source of truth: worker/config.py:12 SUPPORTED_PLATFORMS (not importable
# from the backend venv — it pulls in playwright). Keep in sync with that
# file; same idiom as test_cluster_f.py's WORKER_HANDLER_KEYS.
WORKER_SUPPORTED_PLATFORMS = {"fiverr", "upwork", "peopleperhour", "guru"}


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


def _register(client, email="phase5-api@example.com"):
    r = client.post("/api/auth/register",
                    json={"email": email, "password": "password123", "display_name": "P5"})
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _user_id(Session, email="phase5-api@example.com"):
    db = Session()
    try:
        return db.query(User).filter(User.email == email).first().id
    finally:
        db.close()


def _buyer_request_item(db, uid, status="pending_review"):
    job = Job(user_id=uid, external_id="brief-1", platform="fiverr",
              title="Need a landing page", url="https://www.fiverr.com/briefs/1")
    db.add(job)
    db.commit()
    item = ProposalQueueItem(user_id=uid, job_id=job.id, platform="fiverr",
                             request_type="buyer_request", proposal_text="offer",
                             humanized_text="offer", bid_amount=50,
                             status=status)
    db.add(item)
    db.commit()
    return item


def _fiverr_account(db, uid):
    db.add(PlatformAccount(user_id=uid, platform="fiverr", label="fiverr main",
                           mode="stealth", enabled=True))
    db.commit()


# ---------------- P5-1: buyer-request offer dispatch ----------------

def test_approve_buyer_request_dispatches_stealth_task(client):
    c, Session = client
    token = _register(c)
    uid = _user_id(Session)
    db = Session()
    try:
        _fiverr_account(db, uid)
        item_id = _buyer_request_item(db, uid).id
    finally:
        db.close()

    r = c.post(f"/api/proposals/{item_id}/approve", headers=_auth(token),
               json={"reviewer": "op"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued_for_browser"

    db = Session()
    try:
        task = (db.query(StealthTask)
                .filter(StealthTask.user_id == uid,
                        StealthTask.task_type == "submit_fiverr_offer")
                .one())
        assert task.platform == "fiverr" and task.status == "pending"
        # payload keys the worker's manual_assist handler actually reads
        assert task.payload["proposal_queue_item_id"] == item_id
        assert task.payload["job_url"] == "https://www.fiverr.com/briefs/1"
        assert task.payload["humanized_text"] == "offer"
        assert task.payload["typing_plan"] == []
        assert db.get(ProposalQueueItem, item_id).status == "queued_for_browser"
    finally:
        db.close()


def test_approve_buyer_request_without_account_stays_approved(client):
    c, Session = client
    token = _register(c)
    uid = _user_id(Session)
    db = Session()
    try:
        item_id = _buyer_request_item(db, uid).id
    finally:
        db.close()

    r = c.post(f"/api/proposals/{item_id}/approve", headers=_auth(token),
               json={"reviewer": "op"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"

    db = Session()
    try:
        assert db.query(StealthTask).filter(StealthTask.user_id == uid).count() == 0
        item = db.get(ProposalQueueItem, item_id)
        assert item.status == "approved"
        assert "no fiverr account" in item.submission_result["dispatch_note"]
        audit = (db.query(AuditLog)
                 .filter(AuditLog.action_type == "buyer_request_dispatch_skipped")
                 .one())
        assert audit.detail["reason"] == "no_fiverr_account"
    finally:
        db.close()


def test_approve_buyer_request_circuit_open_stays_approved(client):
    c, Session = client
    token = _register(c)
    uid = _user_id(Session)
    db = Session()
    try:
        _fiverr_account(db, uid)
        item_id = _buyer_request_item(db, uid).id
    finally:
        db.close()

    circuit_breaker.open_circuit("fiverr", "test kill switch", user_id=uid)
    try:
        r = c.post(f"/api/proposals/{item_id}/approve", headers=_auth(token),
                   json={"reviewer": "op"})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "approved"

        db = Session()
        try:
            item = db.get(ProposalQueueItem, item_id)
            assert item.status == "approved"  # not stranded in queued_for_browser
            task = (db.query(StealthTask)
                    .filter(StealthTask.user_id == uid,
                            StealthTask.task_type == "submit_fiverr_offer")
                    .one())
            assert task.status == "skipped_circuit_open"
        finally:
            db.close()
    finally:
        # in-process breaker state must not leak into other tests (user ids
        # restart at 1 in every in-memory db)
        circuit_breaker.close_circuit("fiverr", user_id=uid)


def test_bulk_approve_dispatches_buyer_request(client):
    c, Session = client
    token = _register(c)
    uid = _user_id(Session)
    db = Session()
    try:
        _fiverr_account(db, uid)
        item_id = _buyer_request_item(db, uid).id
    finally:
        db.close()

    r = c.post("/api/proposals/bulk-approve", headers=_auth(token),
               json={"ids": [item_id], "reviewer": "op"})
    assert r.status_code == 200, r.text
    assert r.json()["approved"] == [item_id]

    db = Session()
    try:
        assert db.get(ProposalQueueItem, item_id).status == "queued_for_browser"
        assert (db.query(StealthTask)
                .filter(StealthTask.task_type == "submit_fiverr_offer")
                .count()) == 1
    finally:
        db.close()


# ---------------- P5-1: mark-submitted ----------------

def _plain_item(db, uid, status, platform="peopleperhour"):
    job = Job(user_id=uid, external_id=f"j-{status}", platform=platform, title="J")
    db.add(job)
    db.commit()
    item = ProposalQueueItem(user_id=uid, job_id=job.id, platform=platform,
                             proposal_text="text", status=status)
    db.add(item)
    db.commit()
    return item


def test_mark_submitted_from_approved_and_failed(client):
    c, Session = client
    token = _register(c)
    uid = _user_id(Session)
    db = Session()
    try:
        approved_id = _plain_item(db, uid, "approved").id
        failed_id = _plain_item(db, uid, "failed").id
    finally:
        db.close()

    r = c.post(f"/api/proposals/{approved_id}/mark-submitted", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "submitted"
    assert body["submission_result"]["channel"] == "manual"
    assert body["submission_result"]["manual"] is True

    r = c.post(f"/api/proposals/{failed_id}/mark-submitted", headers=_auth(token),
               json={"channel": "pph_messaging"})
    assert r.status_code == 200, r.text
    assert r.json()["submission_result"]["channel"] == "pph_messaging"

    db = Session()
    try:
        audits = (db.query(AuditLog)
                  .filter(AuditLog.action_type == "proposal_marked_submitted")
                  .all())
        assert len(audits) == 2
        assert audits[0].detail["proposal_id"] == approved_id
    finally:
        db.close()


def test_mark_submitted_rejects_other_statuses(client):
    c, Session = client
    token = _register(c)
    uid = _user_id(Session)
    db = Session()
    try:
        ids = {s: _plain_item(db, uid, s).id
               for s in ("pending_review", "submitted", "queued_for_browser",
                         "rejected")}
    finally:
        db.close()

    for status, item_id in ids.items():
        r = c.post(f"/api/proposals/{item_id}/mark-submitted", headers=_auth(token))
        assert r.status_code == 409, f"{status}: {r.text}"


# ---------------- P5-1: honest 400 replaces the 501 ----------------

def test_submit_non_automated_platform_is_400(client):
    c, Session = client
    token = _register(c)
    uid = _user_id(Session)
    db = Session()
    try:
        item_id = _plain_item(db, uid, "approved", platform="guru").id
    finally:
        db.close()

    r = c.post(f"/api/proposals/{item_id}/submit", headers=_auth(token))
    assert r.status_code == 400, r.text
    assert "isn't automated" in r.json()["detail"]
    assert "Mark as submitted" in r.json()["detail"]
    db = Session()
    try:
        # the failed claim is released — the item can be marked/re-submitted
        assert db.get(ProposalQueueItem, item_id).status == "approved"
    finally:
        db.close()


# ---------------- P5-2: platform registry ----------------

def test_registry_sets_are_consistent():
    served = (WORKER_PLATFORMS | OAUTH_PLATFORMS | STEALTH_CREDENTIAL_PLATFORMS
              | set(DISCOVERY_PLATFORMS) | BROWSER_SYNC_PLATFORMS)
    assert served <= ALL_PLATFORMS
    # upwork is BOTH oauth + stealth (credentials router depends on the overlap)
    assert "upwork" in OAUTH_PLATFORMS
    assert "upwork" in STEALTH_CREDENTIAL_PLATFORMS
    # indeed is accepted by the schema but served nowhere
    assert "indeed" in ALL_PLATFORMS
    assert "indeed" not in served


def test_schema_literal_matches_registry():
    from app.schemas import Platform
    assert set(Platform.__args__) == ALL_PLATFORMS


def test_worker_platform_set_matches_worker_config():
    assert WORKER_PLATFORMS == WORKER_SUPPORTED_PLATFORMS


def test_scattered_lists_are_registry_aliases():
    from app import discovery, gig_analytics, proposal_status_sync
    from app.routers import credentials
    assert discovery.DISCOVERY_PLATFORMS is DISCOVERY_PLATFORMS
    assert gig_analytics.WORKER_PLATFORMS is WORKER_PLATFORMS
    assert proposal_status_sync.BROWSER_SYNC_PLATFORMS is BROWSER_SYNC_PLATFORMS
    assert credentials._OAUTH_PLATFORMS is OAUTH_PLATFORMS
    assert credentials._STEALTH_PLATFORMS is STEALTH_CREDENTIAL_PLATFORMS
