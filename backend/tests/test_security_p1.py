"""Cluster I security-hardening tests: P1-5 IDOR, P1-6 brute-force &
enumeration, P1-7 response hygiene."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.base import AdapterError
from app.database import Base, get_db
from app.main import app
from app.models import (Job, Keyword, KeywordGroup, ProposalQueueItem,
                        SearchFilter, SearchProfile, User)


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
def redis_up():
    """Rate-limiter tests need the real Redis (db 15 per conftest)."""
    import redis as redis_lib

    from app.config import REDIS_URL
    try:
        redis_lib.Redis.from_url(REDIS_URL, socket_timeout=1).ping()
    except Exception:
        pytest.skip("Redis unavailable")


def _register(client, email, password="password123", name="Test User"):
    r = client.post("/api/auth/register",
                    json={"email": email, "password": password, "display_name": name})
    assert r.status_code == 201, r.text
    return r.json()


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _user_id(Session, email):
    db = Session()
    try:
        return db.query(User).filter(User.email == email).one().id
    finally:
        db.close()


# ---------------- P1-5: IDOR ----------------

def _make_group(client, token, name="group"):
    r = client.post("/api/keyword-groups", headers=_auth(token),
                    json={"name": name, "keywords": [{"term": "react", "kind": "primary"}]})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _make_filter(client, token, name="filter"):
    r = client.post("/api/filters", headers=_auth(token), json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_search_profile_foreign_refs_rejected(client):
    c, _ = client
    alice = _register(c, "alice@example.com")["access_token"]
    bob = _register(c, "bob@example.com")["access_token"]
    foreign_group = _make_group(c, alice)
    foreign_filter = _make_filter(c, alice)

    # create with a foreign keyword group / filter → 404 (existence not leaked)
    r = c.post("/api/search-profiles", headers=_auth(bob),
               json={"name": "p", "keyword_group_id": foreign_group})
    assert r.status_code == 404
    r = c.post("/api/search-profiles", headers=_auth(bob),
               json={"name": "p", "filter_id": foreign_filter})
    assert r.status_code == 404
    # nonexistent refs → same 404
    r = c.post("/api/search-profiles", headers=_auth(bob),
               json={"name": "p", "keyword_group_id": 999999})
    assert r.status_code == 404

    # own refs and null refs are fine
    own_group = _make_group(c, bob)
    own_filter = _make_filter(c, bob)
    r = c.post("/api/search-profiles", headers=_auth(bob),
               json={"name": "mine", "keyword_group_id": own_group,
                     "filter_id": own_filter})
    assert r.status_code == 201, r.text
    profile_id = r.json()["id"]
    r = c.post("/api/search-profiles", headers=_auth(bob), json={"name": "plain"})
    assert r.status_code == 201

    # update with foreign refs → 404; clearing to null is allowed
    r = c.put(f"/api/search-profiles/{profile_id}", headers=_auth(bob),
              json={"name": "mine", "keyword_group_id": foreign_group})
    assert r.status_code == 404
    r = c.put(f"/api/search-profiles/{profile_id}", headers=_auth(bob),
              json={"name": "mine", "filter_id": foreign_filter})
    assert r.status_code == 404
    r = c.put(f"/api/search-profiles/{profile_id}", headers=_auth(bob),
              json={"name": "mine", "keyword_group_id": None, "filter_id": None})
    assert r.status_code == 200
    assert r.json()["keyword_group_id"] is None


def test_discovery_foreign_refs_fall_back(client):
    """A profile pointing at another tenant's group/filter (e.g. planted
    before the API checks existed) is treated as if the refs were absent."""
    from app.discovery import DISCOVERY_PLATFORMS
    from app.discovery import platforms_for_profile, search_terms_for_profile
    c, Session = client
    _register(c, "owner@example.com")
    _register(c, "victim@example.com")
    db = Session()
    owner_id = db.query(User).filter(User.email == "owner@example.com").one().id
    victim_id = db.query(User).filter(User.email == "victim@example.com").one().id
    foreign_group = KeywordGroup(user_id=victim_id, name="victim terms",
                                 keywords=[Keyword(term="victimterm", kind="primary")])
    foreign_filter = SearchFilter(user_id=victim_id, name="victim filter",
                                  platforms=["freelancer"])
    db.add_all([foreign_group, foreign_filter])
    db.commit()
    profile = SearchProfile(user_id=owner_id, name="my profile",
                            keyword_group_id=foreign_group.id,
                            filter_id=foreign_filter.id)
    db.add(profile)
    db.commit()

    # foreign group ignored → falls back to the profile name
    assert search_terms_for_profile(db, profile) == ["my profile"]
    # foreign filter ignored → unrestricted platform default
    assert platforms_for_profile(db, profile) == list(DISCOVERY_PLATFORMS)

    # the owner's own refs are still honored
    own_group = KeywordGroup(user_id=owner_id, name="mine",
                             keywords=[Keyword(term="react", kind="primary")])
    own_filter = SearchFilter(user_id=owner_id, name="mine", platforms=["freelancer"])
    db.add_all([own_group, own_filter])
    db.commit()
    profile.keyword_group_id, profile.filter_id = own_group.id, own_filter.id
    db.commit()
    assert search_terms_for_profile(db, profile) == ["react"]
    assert platforms_for_profile(db, profile) == ["freelancer"]
    db.close()


def test_register_gig_foreign_template_rejected(client):
    c, Session = client
    _register(c, "alice@example.com")
    bob = _register(c, "bob@example.com")["access_token"]

    from app.models import GigTemplate
    db = Session()
    foreign = GigTemplate(user_id=_user_id(Session, "alice@example.com"),
                          platform="fiverr", name="alice tpl", template_json={})
    own = GigTemplate(user_id=_user_id(Session, "bob@example.com"),
                      platform="fiverr", name="bob tpl", template_json={})
    db.add_all([foreign, own])
    db.commit()
    foreign_id, own_id = foreign.id, own.id
    db.close()

    body = {"platform": "fiverr", "title": "g", "external_id": "g1"}
    r = c.post("/api/gigs", headers=_auth(bob), json={**body, "template_id": foreign_id})
    assert r.status_code == 404
    r = c.post("/api/gigs", headers=_auth(bob), json={**body, "template_id": 999999})
    assert r.status_code == 404
    r = c.post("/api/gigs", headers=_auth(bob), json={**body, "template_id": own_id})
    assert r.status_code == 201, r.text
    r = c.post("/api/gigs", headers=_auth(bob),
               json={**body, "external_id": "g2"})  # no template → fine
    assert r.status_code == 201


def test_proposal_job_loads_scoped_to_tenant(client):
    """A queue item whose job_id points at another tenant's job (planted
    directly, e.g. by id guessing before scoping existed) must not leak it."""
    c, Session = client
    _register(c, "alice@example.com")
    bob = _register(c, "bob@example.com")["access_token"]
    alice_id = _user_id(Session, "alice@example.com")
    bob_id = _user_id(Session, "bob@example.com")
    db = Session()
    foreign_job = Job(user_id=alice_id, external_id="fj", platform="upwork",
                      title="alice's secret job")
    own_job = Job(user_id=bob_id, external_id="oj", platform="upwork", title="bob job")
    db.add_all([foreign_job, own_job])
    db.flush()
    db.add_all([
        ProposalQueueItem(user_id=bob_id, job_id=foreign_job.id, platform="upwork",
                          proposal_text="x", status="pending_review"),
        ProposalQueueItem(user_id=bob_id, job_id=own_job.id, platform="upwork",
                          proposal_text="y", status="pending_review"),
    ])
    db.commit()
    foreign_item_id = db.query(ProposalQueueItem).filter_by(proposal_text="x").one().id
    db.close()

    items = c.get("/api/proposals", headers=_auth(bob)).json()["items"]
    by_text = {i["proposal_text"]: i for i in items}
    assert by_text["x"]["job"] is None            # foreign job not leaked
    assert by_text["y"]["job"]["title"] == "bob job"
    r = c.get(f"/api/proposals/{foreign_item_id}", headers=_auth(bob))
    assert r.status_code == 200 and r.json()["job"] is None


# ---------------- P1-6: brute-force & enumeration ----------------

def test_successful_logins_never_trip_limiter(client, redis_up):
    c, _ = client
    _register(c, "legit@example.com")
    for _ in range(6):  # limit is 5 — but only failures count
        r = c.post("/api/auth/login",
                   json={"email": "legit@example.com", "password": "password123"})
        assert r.status_code == 200


def test_sixth_failed_login_is_429(client, redis_up):
    c, _ = client
    _register(c, "target@example.com")
    for _ in range(5):
        r = c.post("/api/auth/login",
                   json={"email": "target@example.com", "password": "wrong-pass"})
        assert r.status_code == 401
    r = c.post("/api/auth/login",
               json={"email": "target@example.com", "password": "wrong-pass"})
    assert r.status_code == 429


def test_unknown_email_login_uses_dummy_verify(client, redis_up):
    c, _ = client
    r = c.post("/api/auth/login",
               json={"email": "ghost@example.com", "password": "whatever1"})
    assert r.status_code == 401  # dummy-verify path: 401, not 429/500


def test_register_rate_limit_trips(client, redis_up):
    c, _ = client
    for i in range(5):
        r = c.post("/api/auth/register", json={
            "email": f"user{i}@example.com", "password": "password123",
            "display_name": "U"})
        assert r.status_code == 201, r.text
    r = c.post("/api/auth/register", json={
        "email": "user6@example.com", "password": "password123", "display_name": "U"})
    assert r.status_code == 429


# ---------------- P1-7: response hygiene ----------------

def test_adapter_502_is_generic(client, monkeypatch):
    c, _ = client
    token = _register(c, "adapter@example.com")["access_token"]

    async def boom(self, query, **kwargs):
        raise AdapterError("upstream 500 body: secret-internal-url http://x.internal")

    monkeypatch.setattr("app.adapters.freelancer.FreelancerAdapter.search_jobs", boom)
    r = c.post("/api/adapters/freelancer/search", headers=_auth(token),
               json={"query": "react", "auto_ingest": False})
    assert r.status_code == 502
    assert r.json()["detail"] == "upstream request failed"
    assert "secret" not in r.text


def test_submit_proposal_502_is_generic(client, monkeypatch):
    c, Session = client
    token = _register(c, "submitter@example.com")["access_token"]
    uid = _user_id(Session, "submitter@example.com")
    db = Session()
    job = Job(user_id=uid, external_id="123", platform="freelancer", title="j")
    db.add(job)
    db.flush()
    item = ProposalQueueItem(user_id=uid, job_id=job.id, platform="freelancer",
                             proposal_text="x", status="approved",
                             submission_result={"bidder_id": 1})
    db.add(item)
    db.commit()
    item_id = item.id
    db.close()

    async def boom(self, **kwargs):
        raise RuntimeError("secret upstream url http://api.internal/v0")

    monkeypatch.setattr("app.adapters.freelancer.FreelancerAdapter.place_bid", boom)
    r = c.post(f"/api/proposals/{item_id}/submit", headers=_auth(token))
    assert r.status_code == 502
    assert r.json()["detail"] == "submission failed"
    assert "secret" not in r.text


def test_security_headers_present(client):
    c, _ = client
    r = c.get("/api/health")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert "Strict-Transport-Security" not in r.headers  # off without the TLS flag


def test_hsts_only_behind_tls(client, monkeypatch):
    c, _ = client
    monkeypatch.setattr("app.main.BEHIND_TLS", True)
    r = c.get("/api/health")
    assert r.headers["Strict-Transport-Security"] == \
        "max-age=31536000; includeSubDomains"


def test_cors_wildcard_rejected_outside_dev(monkeypatch):
    from app.auth import validate_auth_config
    monkeypatch.setattr("app.auth.CORS_ORIGINS", ["*"])
    with pytest.raises(RuntimeError, match="CORS_ORIGINS"):
        validate_auth_config()
    monkeypatch.setattr("app.auth.DEV_NOAUTH", True)
    validate_auth_config()  # dev mode: wildcard tolerated


def test_llm_gen_rate_limit_trips(client, redis_up, monkeypatch):
    monkeypatch.setattr("app.gig_templates.llm.llm_available", lambda: False)
    c, _ = client
    token = _register(c, "gen@example.com")["access_token"]
    for _ in range(20):
        r = c.post("/api/gigs/faqs/generate", headers=_auth(token),
                   json={"gig_type": "web", "title": "t"})
        assert r.status_code == 200, r.text
    r = c.post("/api/gigs/faqs/generate", headers=_auth(token),
               json={"gig_type": "web", "title": "t"})
    assert r.status_code == 429


def test_filter_preview_caps_scan(client, monkeypatch):
    monkeypatch.setattr("app.routers.filters.PREVIEW_SCAN_LIMIT", 3)
    c, Session = client
    token = _register(c, "preview@example.com")["access_token"]
    uid = _user_id(Session, "preview@example.com")
    db = Session()
    for i in range(5):
        db.add(Job(user_id=uid, external_id=f"j{i}", platform="upwork", title=f"job {i}"))
    db.commit()
    db.close()
    filter_id = _make_filter(c, token)

    r = c.post(f"/api/filters/{filter_id}/preview", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["matched"]) + body["excluded_count"] == 3  # scan capped, shape kept
