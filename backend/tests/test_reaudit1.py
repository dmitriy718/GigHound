"""Re-audit #1 regression + scalability tests.

N1 template-save shadowing, N2 approve template_id reuse + mint opt-out,
N4 bidder_id persistence, N5 per-user fiverr offer counter, N6 skills/suggest
auth, beat fan-out dispatchers, proposals pagination shape, and the
client_key keyed history lookup.
"""
import pytest
import uuid
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_password
from app.database import Base, get_db
from app.main import app
from app.models import (GigTemplate, Job, PlatformAccount, ProposalQueueItem,
                        SearchProfile, Template, User)


@pytest.fixture(autouse=True)
def force_offline(monkeypatch):
    monkeypatch.setattr("app.proposal_gen.llm.llm_available", lambda: False)
    monkeypatch.setattr("app.routers.jobs.cache._r", None)
    monkeypatch.setattr("app.ingest.cache._r", None)
    monkeypatch.setattr("app.discovery.cache._r", None)


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


def _register(client, email="reaudit@example.com"):
    r = client.post("/api/auth/register",
                    json={"email": email, "password": "password123", "display_name": "RA"})
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _user_id(Session, email="reaudit@example.com"):
    db = Session()
    try:
        return db.query(User).filter(User.email == email).first().id
    finally:
        db.close()


def _make_item(Session, user_id, platform="upwork", status="pending_review", **kw):
    db = Session()
    try:
        external_id = kw.pop("external_id", None) or f"ra-{uuid.uuid4().hex[:12]}"
        job = Job(user_id=user_id, external_id=external_id,
                  platform=platform, title="J")
        db.add(job)
        db.commit()
        fields = dict(user_id=user_id, job_id=job.id, platform=platform,
                      proposal_text="queued proposal text", bid_amount=500.0,
                      status=status, submission_result={})
        fields.update(kw)
        item = ProposalQueueItem(**fields)
        db.add(item)
        db.commit()
        return item.id
    finally:
        db.close()


# ---------------- N1: template save=true no longer 500s ----------------

def test_generate_template_save_true_persists(client, monkeypatch):
    c, Session = client
    from app.textgen import LLMUnavailable

    async def _offline(*a, **kw):
        raise LLMUnavailable("no llm in tests")

    monkeypatch.setattr("app.textgen.generateText", _offline)
    token = _register(c)
    uid = _user_id(Session)

    r = c.post("/api/proposals/templates/generate", headers=_auth(token),
               json={"platform": "upwork", "skills": ["react"], "save": True,
                     "title": "React tpl"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["offline"] is True
    assert body["saved_template_id"]
    db = Session()
    try:
        tpl = db.get(Template, body["saved_template_id"])
        assert tpl is not None and tpl.user_id == uid
        assert tpl.title == "React tpl" and tpl.platform == "upwork"
    finally:
        db.close()


# ---------------- N2: approve with template_id reuse / validation ----------------

def test_approve_with_template_id_reuses_template(client):
    c, Session = client
    token = _register(c)
    uid = _user_id(Session)
    db = Session()
    try:
        tpl = Template(user_id=uid, title="suggested", platform="upwork",
                       text="t", uses=2)
        db.add(tpl)
        db.commit()
        tpl_id = tpl.id
    finally:
        db.close()
    item_id = _make_item(Session, uid)

    r = c.post(f"/api/proposals/{item_id}/approve", headers=_auth(token),
               json={"reviewer": "op", "template_id": tpl_id})
    assert r.status_code == 200, r.text
    db = Session()
    try:
        item = db.get(ProposalQueueItem, item_id)
        assert item.template_id == tpl_id
        # reused, not minted; uses counted at selection only
        assert db.query(Template).filter(Template.user_id == uid).count() == 1
        assert db.get(Template, tpl_id).uses == 2
    finally:
        db.close()


def test_approve_with_foreign_or_missing_template_id_is_404(client):
    c, Session = client
    token = _register(c)
    other = _register(c, "reaudit-other@example.com")
    uid = _user_id(Session)
    other_uid = _user_id(Session, "reaudit-other@example.com")
    db = Session()
    try:
        tpl = Template(user_id=other_uid, title="not yours", platform="upwork", text="t")
        db.add(tpl)
        db.commit()
        foreign_tpl_id = tpl.id
    finally:
        db.close()

    item_id = _make_item(Session, uid)
    r = c.post(f"/api/proposals/{item_id}/approve", headers=_auth(token),
               json={"reviewer": "op", "template_id": foreign_tpl_id})
    assert r.status_code == 404
    r = c.post(f"/api/proposals/{item_id}/approve", headers=_auth(token),
               json={"reviewer": "op", "template_id": 999999})
    assert r.status_code == 404
    # the item stays pending_review after failed approvals
    db = Session()
    try:
        assert db.get(ProposalQueueItem, item_id).status == "pending_review"
    finally:
        db.close()


# ---------------- N4: bidder_id persisted in submission_result ----------------

class _FakeFreelancerAdapter:
    def __init__(self, db, user_id, **kw):
        pass

    async def place_bid(self, **kwargs):
        return {"id": 999}

    async def close(self):
        pass


def test_submit_freelancer_persists_bidder_id(client, monkeypatch):
    c, Session = client
    monkeypatch.setattr("app.adapters.freelancer.FreelancerAdapter", _FakeFreelancerAdapter)
    token = _register(c)
    uid = _user_id(Session)
    db = Session()
    try:
        db.add(PlatformAccount(user_id=uid, platform="freelancer", label="fl",
                               settings={"bidder_id": 4242}))
        db.commit()
    finally:
        db.close()
    item_id = _make_item(Session, uid, platform="freelancer", status="approved",
                         external_id="12345")

    r = c.post(f"/api/proposals/{item_id}/submit", headers=_auth(token))
    assert r.status_code == 200, r.text
    result = r.json()["submission_result"]
    assert result["channel"] == "freelancer_api"
    assert result["bidder_id"] == 4242  # outcome_sync's own-message filter reads this


# ---------------- N5: per-user fiverr offer counter ----------------

def test_fiverr_offer_counter_is_per_user(client):
    c, Session = client
    from app import fiverr_monitor
    from app.fiverr_monitor import offers_remaining_today, process_buyer_requests

    fiverr_monitor._local_counters.clear()
    _register(c, "reaudit-a@example.com")
    _register(c, "reaudit-b@example.com")
    uid_a = _user_id(Session, "reaudit-a@example.com")
    uid_b = _user_id(Session, "reaudit-b@example.com")
    db = Session()
    try:
        for uid in (uid_a, uid_b):
            db.add(GigTemplate(user_id=uid, platform="fiverr", name="gig",
                               template_json={"tags": ["react"]}, is_active=True))
        db.commit()
        requests = [{"id": "br-x", "title": "Need a react dev", "budget": 100,
                     "description": "react work"}]
        assert process_buyer_requests(db, uid_a, requests)["queued"] == 1
        assert offers_remaining_today(uid_a) == 9
        assert offers_remaining_today(uid_b) == 10  # not a global counter
        # B can still queue their own offer the same day
        assert process_buyer_requests(db, uid_b, requests)["queued"] == 1
        assert offers_remaining_today(uid_b) == 9
    finally:
        db.close()
        fiverr_monitor._local_counters.clear()


def test_offers_counter_key_includes_user_id():
    from app.fiverr_monitor import _offers_key

    assert _offers_key(7).startswith("fiverr:offers:7:")
    assert _offers_key(7) != _offers_key(8)


# ---------------- N6: skills/suggest requires auth ----------------

def test_skills_suggest_requires_auth(client):
    c, _ = client
    assert c.get("/api/skills/suggest").status_code == 401
    token = _register(c)
    r = c.get("/api/skills/suggest?q=react", headers=_auth(token))
    assert r.status_code == 200
    assert "suggestions" in r.json()


# ---------------- fan-out beats: dispatchers enqueue per-user/profile tasks ----------------

def test_discovery_tick_dispatches_per_profile(client, monkeypatch):
    c, Session = client
    monkeypatch.setattr("app.tasks.SessionLocal", Session)
    db = Session()
    try:
        u1 = User(email="fan1@example.com", password_hash=hash_password("x"))
        u2 = User(email="fan2@example.com", password_hash=hash_password("x"),
                  is_active=False)
        db.add_all([u1, u2])
        db.commit()
        db.add_all([
            SearchProfile(user_id=u1.id, name="p1"),
            SearchProfile(user_id=u1.id, name="p2"),
            SearchProfile(user_id=u2.id, name="inactive-user-profile"),
        ])
        db.commit()
        keys = {f"{u1.id}:{p.id}" for p in db.query(SearchProfile)
                .filter(SearchProfile.user_id == u1.id).all()}
    finally:
        db.close()

    enqueued = []
    monkeypatch.setattr("app.tasks.discover_profile_task.delay",
                        lambda u, p: enqueued.append(f"{u}:{p}"))
    from app.tasks import discovery_tick_core

    result = discovery_tick_core()
    assert set(result["enqueued"]) == keys  # 2 profiles of the active user only
    assert set(enqueued) == keys


def test_outcome_sync_tick_dispatches_per_user(client, monkeypatch):
    c, Session = client
    monkeypatch.setattr("app.tasks.SessionLocal", Session)
    db = Session()
    try:
        u1 = User(email="os1@example.com", password_hash=hash_password("x"))
        u2 = User(email="os2@example.com", password_hash=hash_password("x"))
        u3 = User(email="os3@example.com", password_hash=hash_password("x"),
                  is_active=False)
        db.add_all([u1, u2, u3])
        db.commit()
        active = {u1.id, u2.id}
    finally:
        db.close()

    enqueued = []
    monkeypatch.setattr("app.tasks.outcome_sync_user_task.delay",
                        lambda u: enqueued.append(u))
    from app.tasks import outcome_sync_tick_core

    result = outcome_sync_tick_core()
    assert set(result["enqueued"]) == active
    assert set(enqueued) == active


# ---------------- proposals list: pagination shape + batch job load ----------------

def test_list_proposals_pagination_shape(client):
    c, Session = client
    token = _register(c)
    uid = _user_id(Session)
    ids = [_make_item(Session, uid) for _ in range(3)]

    r = c.get("/api/proposals", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 3
    assert len(body["items"]) == 3
    assert body["items"][0]["job"]["title"] == "J"  # job still embedded

    page = c.get("/api/proposals?limit=2&offset=2", headers=_auth(token)).json()
    assert page["total"] == 3 and len(page["items"]) == 1

    filtered = c.get("/api/proposals?status=approved", headers=_auth(token)).json()
    assert filtered["total"] == 0 and filtered["items"] == []

    assert c.get("/api/proposals?limit=500", headers=_auth(token)).status_code == 422


# ---------------- client_key: populated at write, keyed history ----------------

def test_client_key_populated_and_history_keyed(client):
    c, Session = client
    from app.client_intel import client_history_for_job, client_key_for

    token = _register(c)
    uid = _user_id(Session)
    db = Session()
    try:
        past = Job(user_id=uid, external_id="ck-1", platform="upwork", title="Old",
                   client_info={"client_id": "cli-77"})
        db.add(past)
        db.commit()
        # listener derived the key at write time
        assert past.client_key == client_key_for(past.client_info, "upwork")
        db.add(ProposalQueueItem(user_id=uid, job_id=past.id, platform="upwork",
                                 proposal_text="t", status="submitted", outcome="hired"))
        new = Job(user_id=uid, external_id="ck-2", platform="upwork", title="New",
                  client_info={"client_id": "cli-77"})
        anon = Job(user_id=uid, external_id="ck-3", platform="upwork", title="Anon",
                   client_info={})
        db.add_all([new, anon])
        db.commit()
        assert new.client_key == past.client_key
        assert anon.client_key is None
        assert client_history_for_job(db, uid, new) == {
            "past_proposals": 1, "hired": 1, "rejected": 0, "ghosted": 0}
        assert client_history_for_job(db, uid, anon) is None
    finally:
        db.close()
