"""Phase 0 security hardening tests: queue-bound write actions, audit
completeness, vault key enforcement, ingest hardening, boolean-query caps,
and the prompt-leak output filter."""
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters import vault
from app.adapters.base import AdapterAuthError
from app.adapters.vault import CredentialVault
from app.boolquery import BooleanQueryError, parse_boolean_query
from app.database import Base, get_db
from app.main import app
from app.models import AuditLog, Job, ProposalQueueItem, User
from app.schemas import JobIngest


@pytest.fixture(autouse=True)
def force_offline(monkeypatch):
    """Deterministic offline paths: no LLM, no Redis rate-limit bucket."""
    monkeypatch.setattr("app.proposal_gen.llm.llm_available", lambda: False)
    monkeypatch.setattr("app.routers.jobs.cache._r", None)


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


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _register(client, email="phase0@example.com"):
    r = client.post("/api/auth/register",
                    json={"email": email, "password": "password123", "display_name": "P0"})
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _user_id(Session, email="phase0@example.com"):
    db = Session()
    try:
        return db.query(User).filter(User.email == email).first().id
    finally:
        db.close()


_item_seq = 0


def _make_item(Session, user_id, platform="upwork", status="pending_review",
               text="queued proposal text", bid=500.0, needs_review=False,
               submission_result=None, reviewed_by=None, versions=None):
    global _item_seq
    _item_seq += 1
    db = Session()
    try:
        job = Job(user_id=user_id, external_id=str(10000 + _item_seq),
                  platform=platform, title=f"Job {_item_seq}")
        db.add(job)
        db.commit()
        item = ProposalQueueItem(
            user_id=user_id, job_id=job.id, platform=platform,
            proposal_text=text, bid_amount=bid, status=status,
            needs_review=needs_review, reviewed_by=reviewed_by,
            submission_result=submission_result or {},
            versions=versions or [],
        )
        db.add(item)
        db.commit()
        return item.id
    finally:
        db.close()


def _audit_rows(Session, action_type):
    db = Session()
    try:
        return db.query(AuditLog).filter(AuditLog.action_type == action_type).all()
    finally:
        db.close()


# ---------------- bulk_approve: audit + templates + versions, needs_review guard ----------------

def test_bulk_approve_writes_audit_templates_versions(client):
    c, Session = client
    token = _register(c)
    uid = _user_id(Session)
    ok = _make_item(Session, uid)
    flagged = _make_item(Session, uid, needs_review=True)
    already = _make_item(Session, uid, status="approved")

    r = c.post("/api/proposals/bulk-approve", headers=_auth(token),
               json={"ids": [ok, flagged, already], "reviewer": "operator"})
    assert r.status_code == 200
    assert r.json() == {"approved": [ok], "skipped": [flagged, already]}

    db = Session()
    try:
        item = db.get(ProposalQueueItem, ok)
        assert item.status == "approved" and item.reviewed_by == "operator"
        assert item.template_id is not None
        assert item.versions[-1]["by"] == "operator"
        assert item.versions[-1]["text"] == "queued proposal text"
        from app.models import Template
        tpl = db.get(Template, item.template_id)
        assert tpl is not None and tpl.text == "queued proposal text"
        logs = db.query(AuditLog).filter(
            AuditLog.action_type == "proposal_approved",
            AuditLog.user_id == uid).all()
        assert len(logs) == 1
        assert logs[0].detail["proposal_id"] == ok
        assert logs[0].detail["approved_by"] == "operator"
    finally:
        db.close()


def test_bulk_approve_requires_reviewer(client):
    c, Session = client
    token = _register(c)
    r = c.post("/api/proposals/bulk-approve", headers=_auth(token),
               json={"ids": [1], "reviewer": ""})
    assert r.status_code == 400


# ---------------- revert: post-approval mutation re-enters review ----------------

def test_revert_resets_status_when_content_changes(client):
    c, Session = client
    token = _register(c)
    uid = _user_id(Session)
    item_id = _make_item(
        Session, uid, status="approved", text="edited text", bid=700.0,
        reviewed_by="operator",
        versions=[{"text": "original text", "bid": 500.0, "by": "generator",
                   "at": "2026-08-29T00:00:00+00:00"}])

    r = c.post(f"/api/proposals/{item_id}/revert", headers=_auth(token),
               json={"version_index": 0})
    assert r.status_code == 200
    body = r.json()
    assert body["proposal_text"] == "original text"
    assert body["bid_amount"] == 500.0
    assert body["status"] == "pending_review"
    assert body["reviewed_by"] is None and body["reviewed_at"] is None


def test_revert_keeps_status_when_nothing_changes(client):
    c, Session = client
    token = _register(c)
    uid = _user_id(Session)
    item_id = _make_item(
        Session, uid, status="approved", text="same text", bid=500.0,
        reviewed_by="operator",
        versions=[{"text": "same text", "bid": 500.0, "by": "generator",
                   "at": "2026-08-29T00:00:00+00:00"}])

    r = c.post(f"/api/proposals/{item_id}/revert", headers=_auth(token),
               json={"version_index": 0})
    assert r.status_code == 200
    assert r.json()["status"] == "approved"
    assert r.json()["reviewed_by"] == "operator"


def test_revert_allows_pending_review(client):
    c, Session = client
    token = _register(c)
    uid = _user_id(Session)
    item_id = _make_item(
        Session, uid, status="pending_review", text="edited text",
        versions=[{"text": "original text", "bid": 500.0, "by": "generator",
                   "at": "2026-08-29T00:00:00+00:00"}])

    r = c.post(f"/api/proposals/{item_id}/revert", headers=_auth(token),
               json={"version_index": 0})
    assert r.status_code == 200
    assert r.json()["proposal_text"] == "original text"
    assert r.json()["status"] == "pending_review"


def test_revert_rejects_submitted_item(client):
    """Reverting a submitted item would flip it back to pending_review, from
    which approve → submit sends a SECOND bid for the same job."""
    c, Session = client
    token = _register(c)
    uid = _user_id(Session)
    item_id = _make_item(
        Session, uid, status="submitted", text="sent text",
        versions=[{"text": "original text", "bid": 500.0, "by": "generator",
                   "at": "2026-08-29T00:00:00+00:00"}])

    r = c.post(f"/api/proposals/{item_id}/revert", headers=_auth(token),
               json={"version_index": 0})
    assert r.status_code == 409
    assert "submitted" in r.json()["detail"]
    db = Session()
    try:
        item = db.get(ProposalQueueItem, item_id)
        assert item.status == "submitted" and item.proposal_text == "sent text"
    finally:
        db.close()


# ---------------- approve: empty text guard ----------------

def test_approve_rejects_empty_proposal_text(client):
    c, Session = client
    token = _register(c)
    uid = _user_id(Session)
    item_id = _make_item(Session, uid, text="real draft text")

    for blank in ("", "   \n\t "):
        r = c.post(f"/api/proposals/{item_id}/approve", headers=_auth(token),
                   json={"reviewer": "operator", "proposal_text": blank})
        assert r.status_code == 422, r.text

    db = Session()
    try:
        item = db.get(ProposalQueueItem, item_id)
        assert item.status == "pending_review"
        assert item.proposal_text == "real draft text"
    finally:
        db.close()

    # a normal approve still works
    r = c.post(f"/api/proposals/{item_id}/approve", headers=_auth(token),
               json={"reviewer": "operator"})
    assert r.status_code == 200
    assert r.json()["status"] == "approved"


# ---------------- adapter write endpoints are approval-bound ----------------

class _FakeFreelancerAdapter:
    calls = []

    def __init__(self, db, user_id, **kw):
        pass

    async def place_bid(self, **kwargs):
        type(self).calls.append(kwargs)
        return {"id": 999}

    def bids_remaining(self):
        return 10

    async def close(self):
        pass


class _FakeUpworkAdapter:
    calls = []

    def __init__(self, db, user_id, **kw):
        pass

    def submit_proposal(self, **kwargs):
        type(self).calls.append(kwargs)
        return {"id": "rec-1", "status": "pending_browser_execution"}

    async def close(self):
        pass


def test_freelancer_bid_requires_approved_queue_item(client, monkeypatch):
    c, Session = client
    _FakeFreelancerAdapter.calls = []
    monkeypatch.setattr("app.routers.adapters.FreelancerAdapter", _FakeFreelancerAdapter)
    token = _register(c)
    uid = _user_id(Session)
    pending = _make_item(Session, uid, platform="freelancer",
                         submission_result={"bidder_id": 777})

    # pending_review item → 409, adapter never called
    r = c.post("/api/adapters/freelancer/bid", headers=_auth(token),
               json={"proposal_queue_item_id": pending})
    assert r.status_code == 409
    # unknown item → 404
    assert c.post("/api/adapters/freelancer/bid", headers=_auth(token),
                  json={"proposal_queue_item_id": 99999}).status_code == 404
    assert _FakeFreelancerAdapter.calls == []

    # approve the item (as the queue would), then the bid goes out with
    # EXACTLY the queued text/bid — the caller supplies neither
    db = Session()
    item = db.get(ProposalQueueItem, pending)
    item.status = "approved"
    item.reviewed_by = "operator"
    db.commit()
    db.close()

    r = c.post("/api/adapters/freelancer/bid", headers=_auth(token),
               json={"proposal_queue_item_id": pending})
    assert r.status_code == 200, r.text
    assert r.json()["bid"] == {"id": 999}
    call = _FakeFreelancerAdapter.calls[0]
    assert call["proposal"] == "queued proposal text"
    assert call["amount"] == 500.0
    logs = _audit_rows(Session, "bid_placed")
    assert len(logs) == 1 and logs[0].detail["proposal_queue_item_id"] == pending


def test_freelancer_bid_rejects_other_users_item(client, monkeypatch):
    c, Session = client
    _FakeFreelancerAdapter.calls = []
    monkeypatch.setattr("app.routers.adapters.FreelancerAdapter", _FakeFreelancerAdapter)
    alice = _register(c, "alice@example.com")
    bob = _register(c, "bob@example.com")
    alice_uid = _user_id(Session, "alice@example.com")
    item_id = _make_item(Session, alice_uid, platform="freelancer",
                         status="approved", submission_result={"bidder_id": 777})
    r = c.post("/api/adapters/freelancer/bid", headers=_auth(bob),
               json={"proposal_queue_item_id": item_id})
    assert r.status_code == 404
    assert _FakeFreelancerAdapter.calls == []


def test_upwork_proposals_requires_approved_queue_item(client, monkeypatch):
    c, Session = client
    _FakeUpworkAdapter.calls = []
    monkeypatch.setattr("app.routers.adapters.UpworkAgencyAdapter", _FakeUpworkAdapter)
    token = _register(c)
    uid = _user_id(Session)
    item_id = _make_item(Session, uid, status="approved", reviewed_by="operator",
                         submission_result={"on_behalf_of": "jane", "connects_required": 6})

    r = c.post("/api/adapters/upwork/proposals", headers=_auth(token),
               json={"proposal_queue_item_id": item_id})
    assert r.status_code == 200, r.text
    call = _FakeUpworkAdapter.calls[0]
    assert call["proposal_text"] == "queued proposal text"
    assert call["approved_by"] == "operator"
    assert call["on_behalf_of"] == "jane"
    logs = _audit_rows(Session, "proposal_queued")
    assert len(logs) == 1 and logs[0].detail["proposal_queue_item_id"] == item_id

    # the item waits for the browser worker (same contract as /submit)
    db = Session()
    try:
        assert db.get(ProposalQueueItem, item_id).status == "queued_for_browser"
    finally:
        db.close()

    # a pending item cannot be queued
    pending = _make_item(Session, uid)
    assert c.post("/api/adapters/upwork/proposals", headers=_auth(token),
                  json={"proposal_queue_item_id": pending}).status_code == 409


def test_upwork_submit_paths_409_when_circuit_open(client, monkeypatch):
    from app import circuit_breaker

    c, Session = client
    _FakeUpworkAdapter.calls = []
    monkeypatch.setattr("app.routers.adapters.UpworkAgencyAdapter", _FakeUpworkAdapter)
    monkeypatch.setattr("app.adapters.upwork_agency.UpworkAgencyAdapter", _FakeUpworkAdapter)
    token = _register(c)
    uid = _user_id(Session)
    adapter_item = _make_item(Session, uid, status="approved", reviewed_by="operator",
                              submission_result={"on_behalf_of": "jane"})
    submit_item = _make_item(Session, uid, status="approved", reviewed_by="operator",
                             submission_result={"on_behalf_of": "jane"})

    circuit_breaker.open_circuit("upwork", "manual halt")
    try:
        # adapters path: 409, item stays approved, no audit row
        r = c.post("/api/adapters/upwork/proposals", headers=_auth(token),
                   json={"proposal_queue_item_id": adapter_item})
        assert r.status_code == 409
        assert "circuit OPEN for upwork" in r.json()["detail"]

        # proposals /submit path: same guard
        r = c.post(f"/api/proposals/{submit_item}/submit", headers=_auth(token))
        assert r.status_code == 409
        assert "circuit OPEN for upwork" in r.json()["detail"]
    finally:
        circuit_breaker.close_circuit("upwork", "test cleanup")

    db = Session()
    try:
        assert db.get(ProposalQueueItem, adapter_item).status == "approved"
        assert db.get(ProposalQueueItem, submit_item).status == "approved"
        from app.models import StealthTask
        skipped = db.query(StealthTask).filter(
            StealthTask.status == "skipped_circuit_open").all()
        assert len(skipped) == 2  # recorded for UI visibility, but never queued
    finally:
        db.close()


# ---------------- submit: audit row + upwork adapter close ----------------

def test_submit_success_writes_audit_and_closes_adapter(client, monkeypatch):
    c, Session = client
    closed = []

    class _ClosingUpwork(_FakeUpworkAdapter):
        async def close(self):
            closed.append(True)

    monkeypatch.setattr("app.adapters.upwork_agency.UpworkAgencyAdapter", _ClosingUpwork)
    token = _register(c)
    uid = _user_id(Session)
    item_id = _make_item(Session, uid, status="approved", reviewed_by="operator",
                         submission_result={"on_behalf_of": "jane"})

    r = c.post(f"/api/proposals/{item_id}/submit", headers=_auth(token))
    assert r.status_code == 200, r.text
    # upwork waits for the external browser worker — not "submitted" yet
    assert r.json()["status"] == "queued_for_browser"
    assert closed == [True]  # upwork branch now closes its HTTP client
    logs = _audit_rows(Session, "proposal_submitted")
    assert len(logs) == 1
    assert logs[0].platform == "upwork"
    assert logs[0].detail["proposal_id"] == item_id
    assert logs[0].detail["channel"] == "upwork_agency_queue"
    assert logs[0].detail["platform_response_id"] == "rec-1"


def test_duplicate_submit_409s_after_atomic_claim(client, monkeypatch):
    """The second submit (double-click / client retry) must lose the atomic
    approved → submitting claim instead of placing a second bid."""
    c, Session = client
    _FakeUpworkAdapter.calls = []
    monkeypatch.setattr("app.adapters.upwork_agency.UpworkAgencyAdapter", _FakeUpworkAdapter)
    token = _register(c)
    uid = _user_id(Session)
    item_id = _make_item(Session, uid, status="approved", reviewed_by="operator",
                         submission_result={"on_behalf_of": "jane"})

    r = c.post(f"/api/proposals/{item_id}/submit", headers=_auth(token))
    assert r.status_code == 200, r.text
    r = c.post(f"/api/proposals/{item_id}/submit", headers=_auth(token))
    assert r.status_code == 409
    assert "already submitted or in flight" in r.json()["detail"]
    assert len(_FakeUpworkAdapter.calls) == 1  # exactly one external submission

    # a pre-dispatch 409 (circuit open) releases the claim back to approved
    from app import circuit_breaker
    retry_item = _make_item(Session, uid, status="approved", reviewed_by="operator",
                            submission_result={"on_behalf_of": "jane"})
    circuit_breaker.open_circuit("upwork", "manual halt")
    try:
        r = c.post(f"/api/proposals/{retry_item}/submit", headers=_auth(token))
        assert r.status_code == 409
    finally:
        circuit_breaker.close_circuit("upwork", "test cleanup")
    db = Session()
    try:
        assert db.get(ProposalQueueItem, retry_item).status == "approved"
    finally:
        db.close()


# ---------------- ingest hardening ----------------

def test_job_ingest_rejects_non_http_urls():
    with pytest.raises(ValidationError):
        JobIngest(external_id="x", platform="upwork", title="t", url="javascript:alert(1)")
    with pytest.raises(ValidationError):
        JobIngest(external_id="x", platform="upwork", title="t", url="file:///etc/passwd")
    JobIngest(external_id="x", platform="upwork", title="t", url="")
    JobIngest(external_id="x", platform="upwork", title="t", url="https://example.com/job")


def test_ingest_endpoint_validates_body(client):
    c, _ = client
    token = _register(c)
    # non-http url → 422, not 500
    r = c.post("/api/jobs/ingest", headers=_auth(token), json={"jobs": [
        {"external_id": "j-1", "platform": "upwork", "title": "x",
         "url": "javascript:alert(1)"}]})
    assert r.status_code == 422
    # malformed job entries → 422
    r = c.post("/api/jobs/ingest", headers=_auth(token),
               json={"jobs": [{"nope": 1}]})
    assert r.status_code == 422
    # wrong shape entirely → 422
    assert c.post("/api/jobs/ingest", headers=_auth(token),
                  json="not-a-dict").status_code == 422


def test_ingest_rate_limit(client, monkeypatch):
    class _FakeRedis:
        def __init__(self):
            self.counts = {}

        def incr(self, key):
            self.counts[key] = self.counts.get(key, 0) + 1
            return self.counts[key]

        def expire(self, key, ttl):
            pass

        def get(self, key):  # JWT denylist lookups on the auth path
            return None

        def scan_iter(self, pattern):
            return iter([])

        def delete(self, *keys):
            pass

    monkeypatch.setattr("app.routers.jobs.cache._r", _FakeRedis())
    c, _ = client
    token = _register(c)
    for _ in range(30):
        r = c.post("/api/jobs/ingest", headers=_auth(token), json={"jobs": []})
        assert r.status_code == 200
    r = c.post("/api/jobs/ingest", headers=_auth(token), json={"jobs": []})
    assert r.status_code == 429


# ---------------- boolean query caps ----------------

def test_boolean_query_length_cap():
    with pytest.raises(BooleanQueryError, match="too long"):
        parse_boolean_query("react AND " * 200)


def test_boolean_query_depth_cap():
    with pytest.raises(BooleanQueryError, match="nesting too deep"):
        parse_boolean_query("NOT " * 40 + "react")
    with pytest.raises(BooleanQueryError, match="nesting too deep"):
        parse_boolean_query("(" * 40 + "react" + ")" * 40)
    # at the limit it still parses fine
    assert parse_boolean_query("NOT " * 32 + "react") is not None


def test_boolean_caps_enforced_at_profile_api(client):
    c, _ = client
    token = _register(c)
    r = c.post("/api/search-profiles", headers=_auth(token),
               json={"name": "deep", "boolean_query": "NOT " * 40 + "react"})
    assert r.status_code == 422
    r = c.post("/api/search-profiles/validate-boolean", headers=_auth(token),
               json={"query": "x " * 600})
    assert r.json()["valid"] is False


# ---------------- vault ----------------

def test_vault_invalid_token_raises_clean_auth_error(db, monkeypatch):
    user = User(email="vault@example.com", password_hash="x")
    db.add(user)
    db.commit()
    monkeypatch.setenv("GIGHOUND_VAULT_KEY", Fernet.generate_key().decode())
    CredentialVault(db, user.id).store("freelancer", "default", {"k": "v"})
    # key rotation / wrong key → clean re-enroll error, not a crypto traceback
    monkeypatch.setenv("GIGHOUND_VAULT_KEY", Fernet.generate_key().decode())
    with pytest.raises(AdapterAuthError, match="re-enroll credentials"):
        CredentialVault(db, user.id).load("freelancer", "default")


def test_vault_fails_fast_without_key_outside_dev(monkeypatch):
    monkeypatch.delenv("GIGHOUND_VAULT_KEY", raising=False)
    monkeypatch.delenv("GIGHUNTER_VAULT_KEY", raising=False)
    monkeypatch.delenv("GIGHOUND_DEV_NOAUTH", raising=False)
    with pytest.raises(RuntimeError, match="GIGHOUND_VAULT_KEY"):
        vault._fernet()


def test_vault_dev_key_persisted_across_calls(monkeypatch, tmp_path):
    monkeypatch.delenv("GIGHOUND_VAULT_KEY", raising=False)
    monkeypatch.delenv("GIGHUNTER_VAULT_KEY", raising=False)
    monkeypatch.setenv("GIGHOUND_DEV_NOAUTH", "1")
    key_file = tmp_path / ".vault-dev-key"
    monkeypatch.setattr(vault, "_DEV_KEY_FILE", key_file)
    first = vault._fernet()
    assert key_file.exists()
    assert key_file.stat().st_mode & 0o777 == 0o600
    second = vault._fernet()  # "restart" — same key, same secrets readable
    assert second._signing_key == first._signing_key


# ---------------- prompt-leak output filter ----------------

def test_output_filter_strips_prompt_internals():
    from app.proposal_gen import _strip_prompt_leakage
    text = "Hi, I can help with this.\nRATE CONTEXT: $75/hr. Suggested bid: $5.\nLet's talk."
    clean, warning = _strip_prompt_leakage(text, "$75/hr")
    assert "RATE CONTEXT" not in clean and "$75/hr" not in clean
    assert "Let's talk." in clean
    assert warning is not None
    clean2, warning2 = _strip_prompt_leakage("Hi, I can help.\nLet's talk.", "$75/hr")
    assert warning2 is None and clean2 == "Hi, I can help.\nLet's talk."


@pytest.mark.asyncio
async def test_generate_flags_leaking_llm_draft(db, monkeypatch):
    from app import proposal_gen

    user = User(email="gen@example.com", password_hash="x")
    db.add(user)
    db.commit()
    job = Job(user_id=user.id, external_id="g1", platform="upwork",
              title="React dashboard", description="build a react dashboard",
              skills=["React"], job_type="fixed")
    db.add(job)
    db.commit()

    monkeypatch.setattr("app.proposal_gen.llm.llm_available", lambda: True)

    async def fake_complete(system, user_prompt, **kw):
        return {"text": "Great fit for this project.\n"
                        "RATE CONTEXT: $50/hr. Suggested bid: $5.\n"
                        "SKILL GAPS (do NOT claim these): none",
                "model": "m", "provider": "p", "latency_ms": 1}

    monkeypatch.setattr("app.proposal_gen.llm.complete", fake_complete)
    result = await proposal_gen.generate(db, job)
    assert result["leak_warning"] is not None
    assert result["needs_review"] is True
    assert "RATE CONTEXT" not in result["draft_text"]
    assert "SKILL GAPS" not in result["draft_text"]
    assert "Great fit for this project." in result["draft_text"]
