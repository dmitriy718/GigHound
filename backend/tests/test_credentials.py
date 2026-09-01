"""Tests for credential enrollment: enroll/status/delete round-trip, key
validation, credential_ref auto-generation, tenancy, Freelancer OAuth, and
the worker-facing stealth-session endpoint."""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.freelancer import FreelancerAdapter
from app.adapters.vault import CredentialVault
from app.auth import create_access_token, hash_password
from app.database import Base, get_db
from app.main import app
from app.models import AuditLog, PlatformAccount, StealthTask, User

WORKER_HEADERS = {"Authorization": "Bearer test-worker-token"}

STORAGE_STATE = {
    "cookies": [{"name": "session", "value": "abc123secret", "domain": ".fiverr.com",
                 "path": "/", "expires": -1, "httpOnly": True, "secure": True,
                 "sameSite": "Lax"}],
    "origins": [{"origin": "https://www.fiverr.com",
                 "localStorage": [{"name": "k", "value": "v"}]}],
}


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


def _headers(user):
    return {"Authorization": f"Bearer {create_access_token(user)}"}


def _account(db, user_id, platform="fiverr", principal="default", credential_ref=""):
    a = PlatformAccount(user_id=user_id, platform=platform, label=f"{platform} acct",
                        principal=principal, mode="stealth" if platform == "fiverr" else "api",
                        credential_ref=credential_ref)
    db.add(a)
    db.commit()
    return a


def _claimed_task(db, user_id, platform="fiverr"):
    """Session reads are scoped to an active claim (the worker fetches the
    session only while executing a claimed task)."""
    t = StealthTask(user_id=user_id, platform=platform, task_type="fetch_metrics",
                    payload={}, status="claimed", claimed_by="w-1")
    db.add(t)
    db.commit()
    return t


# ---------------- enroll / status / delete ----------------

def test_enroll_status_delete_round_trip(client):
    c, Session = client
    db = Session()
    u = _user(db, "enroll@example.com")
    acct = _account(db, u.id)
    h = _headers(u)

    r = c.post(f"/api/accounts/{acct.id}/credentials",
               json={"secrets": {"storage_state_json": json.dumps(STORAGE_STATE)}},
               headers=h)
    assert r.status_code == 204, r.text

    # credential_ref auto-generated and persisted
    db.refresh(acct)
    assert acct.credential_ref == "vault://fiverr/default"

    r = c.get(f"/api/accounts/{acct.id}/credentials/status", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["enrolled"] is True
    assert body["keys"] == ["storage_state_json"]
    assert body["updated_at"] is not None
    # values are never exposed
    assert "abc123secret" not in r.text

    # audit trail: keys only
    log_row = (db.query(AuditLog)
               .filter_by(user_id=u.id, action_type="credentials_enrolled").one())
    assert log_row.detail == {"account_id": acct.id, "keys": ["storage_state_json"]}

    r = c.delete(f"/api/accounts/{acct.id}/credentials", headers=h)
    assert r.status_code == 204
    db.refresh(acct)
    assert acct.credential_ref == ""
    r = c.get(f"/api/accounts/{acct.id}/credentials/status", headers=h)
    assert r.json() == {"enrolled": False, "keys": [], "updated_at": None}
    assert (db.query(AuditLog)
            .filter_by(user_id=u.id, action_type="credentials_deleted").count()) == 1


def test_enroll_username_password_and_existing_ref_kept(client):
    c, Session = client
    db = Session()
    u = _user(db, "userpass@example.com")
    acct = _account(db, u.id, platform="guru", credential_ref="vault://guru/default")
    r = c.post(f"/api/accounts/{acct.id}/credentials",
               json={"secrets": {"username": "me@example.com", "password": "hunter2"}},
               headers=_headers(u))
    assert r.status_code == 204, r.text
    db.refresh(acct)
    assert acct.credential_ref == "vault://guru/default"  # untouched
    r = c.get(f"/api/accounts/{acct.id}/credentials/status", headers=_headers(u))
    assert r.json()["keys"] == ["password", "username"]
    assert "hunter2" not in r.text


def test_enroll_freelancer_tokens(client):
    c, Session = client
    db = Session()
    u = _user(db, "fl-tokens@example.com")
    acct = _account(db, u.id, platform="freelancer")
    r = c.post(f"/api/accounts/{acct.id}/credentials",
               json={"secrets": {"access_token": "tok", "refresh_token": "ref"}},
               headers=_headers(u))
    assert r.status_code == 204, r.text
    db.refresh(acct)
    assert acct.credential_ref == "vault://freelancer/default"
    assert CredentialVault(db, u.id).load("freelancer", "default") == {
        "access_token": "tok", "refresh_token": "ref"}


# ---------------- upwork: BOTH oauth and stealth credential types ----------------

def test_enroll_upwork_browser_session(client):
    """The worker drives upwork via the browser, so storage_state enrollment
    must work through the product (not just the ops-only login CLI)."""
    c, Session = client
    db = Session()
    u = _user(db, "uw-state@example.com")
    acct = _account(db, u.id, platform="upwork")
    r = c.post(f"/api/accounts/{acct.id}/credentials",
               json={"secrets": {"storage_state_json": json.dumps(STORAGE_STATE)}},
               headers=_headers(u))
    assert r.status_code == 204, r.text
    r = c.get(f"/api/accounts/{acct.id}/credentials/status", headers=_headers(u))
    assert r.json()["keys"] == ["storage_state_json"]

    # and the worker can actually consume it via the stealth-session endpoint
    _claimed_task(db, u.id, platform="upwork")
    r = c.get(f"/api/gigs/stealth-session?platform=upwork&user_id={u.id}",
              headers=WORKER_HEADERS)
    assert r.json()["storage_state"] == STORAGE_STATE


def test_enroll_upwork_userpass_and_tokens(client):
    c, Session = client
    db = Session()
    u = _user(db, "uw-dual@example.com")
    acct = _account(db, u.id, platform="upwork", principal="up")
    r = c.post(f"/api/accounts/{acct.id}/credentials",
               json={"secrets": {"username": "me@example.com", "password": "hunter2"}},
               headers=_headers(u))
    assert r.status_code == 204, r.text

    # API tokens still enroll on the same platform (oauth branch preserved)
    acct2 = _account(db, u.id, platform="upwork", principal="api")
    r = c.post(f"/api/accounts/{acct2.id}/credentials",
               json={"secrets": {"access_token": "tok", "refresh_token": "ref"}},
               headers=_headers(u))
    assert r.status_code == 204, r.text
    assert CredentialVault(db, u.id).load("upwork", "api") == {
        "access_token": "tok", "refresh_token": "ref"}

    # upwork accepts the union of keys but rejects anything outside it
    r = c.post(f"/api/accounts/{acct2.id}/credentials",
               json={"secrets": {"access_token": "tok", "api_secret": "zzz"}},
               headers=_headers(u))
    assert r.status_code == 422


def test_enroll_linkedin_indeed_stealth_rejected(client):
    """No worker serves linkedin/indeed — stealth enrollment is not supported."""
    c, Session = client
    db = Session()
    u = _user(db, "unsupported@example.com")
    for platform in ("linkedin", "indeed"):
        acct = _account(db, u.id, platform=platform, principal=platform)
        r = c.post(f"/api/accounts/{acct.id}/credentials",
                   json={"secrets": {"storage_state_json": json.dumps(STORAGE_STATE)}},
                   headers=_headers(u))
        assert r.status_code == 422, r.text
        assert "not supported" in r.json()["detail"]
        db.refresh(acct)
        assert acct.credential_ref == ""


# ---------------- validation ----------------

@pytest.mark.parametrize("platform,secrets", [
    ("fiverr", {}),                                                   # empty dict
    ("fiverr", {"storage_state_json": "{not json"}),                  # invalid JSON
    ("fiverr", {"storage_state_json": '["list"]'}),                   # not an object
    ("fiverr", {"username": "x"}),                                    # password missing
    ("fiverr", {"storage_state_json": json.dumps(STORAGE_STATE),
                "username": "x", "password": "y"}),                   # both forms
    ("fiverr", {"api_key": "zzz"}),                                   # unknown key
    ("freelancer", {"refresh_token": "ref"}),                         # access_token required
    ("freelancer", {"access_token": "t", "client_secret": "s"}),      # unknown key
    ("upwork", {"password": "x"}),                                    # unknown for oauth platform
])
def test_enroll_validation_422(client, platform, secrets):
    c, Session = client
    db = Session()
    u = _user(db, f"val-{platform}@example.com")
    acct = _account(db, u.id, platform=platform)
    r = c.post(f"/api/accounts/{acct.id}/credentials",
               json={"secrets": secrets}, headers=_headers(u))
    assert r.status_code == 422, r.text
    db.refresh(acct)
    assert acct.credential_ref == ""  # nothing persisted on failure


# ---------------- tenancy ----------------

def test_cross_tenant_404(client):
    c, Session = client
    db = Session()
    u1 = _user(db, "owner@example.com")
    u2 = _user(db, "intruder@example.com")
    acct = _account(db, u1.id)
    h2 = _headers(u2)

    assert c.post(f"/api/accounts/{acct.id}/credentials",
                  json={"secrets": {"storage_state_json": json.dumps(STORAGE_STATE)}},
                  headers=h2).status_code == 404
    assert c.get(f"/api/accounts/{acct.id}/credentials/status",
                 headers=h2).status_code == 404
    assert c.delete(f"/api/accounts/{acct.id}/credentials",
                    headers=h2).status_code == 404
    # and anonymous
    assert c.get(f"/api/accounts/{acct.id}/credentials/status").status_code == 401


# ---------------- Freelancer OAuth ----------------

def test_oauth_start_unconfigured_501(client, monkeypatch):
    c, Session = client
    db = Session()
    u = _user(db, "oauth501@example.com")
    acct = _account(db, u.id, platform="freelancer")
    monkeypatch.delenv("FREELANCER_CLIENT_ID", raising=False)
    monkeypatch.delenv("FREELANCER_CLIENT_SECRET", raising=False)

    r = c.get(f"/api/accounts/{acct.id}/oauth/freelancer/start", headers=_headers(u))
    assert r.status_code == 501
    assert "FREELANCER_CLIENT_ID" in r.json()["detail"]


def test_oauth_start_returns_authorize_url(client, monkeypatch):
    c, Session = client
    db = Session()
    u = _user(db, "oauthstart@example.com")
    acct = _account(db, u.id, platform="freelancer")
    monkeypatch.setenv("FREELANCER_CLIENT_ID", "cid-123")
    monkeypatch.setenv("FREELANCER_CLIENT_SECRET", "sekret")

    r = c.get(f"/api/accounts/{acct.id}/oauth/freelancer/start", headers=_headers(u))
    assert r.status_code == 200
    url = r.json()["authorize_url"]
    assert url.startswith("https://accounts.freelancer.com/oauth/authorise?")
    assert "client_id=cid-123" in url

    # non-freelancer account rejected
    fiverr_acct = _account(db, u.id, platform="fiverr")
    r = c.get(f"/api/accounts/{fiverr_acct.id}/oauth/freelancer/start",
              headers=_headers(u))
    assert r.status_code == 400


def test_oauth_complete_stores_tokens(client, monkeypatch):
    c, Session = client
    db = Session()
    u = _user(db, "oauthdone@example.com")
    acct = _account(db, u.id, platform="freelancer")
    monkeypatch.setenv("FREELANCER_CLIENT_ID", "cid-123")
    monkeypatch.setenv("FREELANCER_CLIENT_SECRET", "sekret")

    async def fake_exchange(self, client_id, client_secret, code, redirect_uri):
        assert code == "auth-code-1"
        return self._persist_tokens(
            {"access_token": "fl-access", "refresh_token": "fl-refresh",
             "expires_in": 3600}, client_id, client_secret)

    monkeypatch.setattr(FreelancerAdapter, "exchange_code", fake_exchange)
    r = c.post(f"/api/accounts/{acct.id}/oauth/freelancer/complete",
               json={"code": "auth-code-1"}, headers=_headers(u))
    assert r.status_code == 204, r.text

    db.refresh(acct)
    assert acct.credential_ref == "vault://freelancer/default"
    creds = CredentialVault(db, u.id).load("freelancer", "default")
    assert creds["access_token"] == "fl-access"
    assert creds["refresh_token"] == "fl-refresh"
    assert creds["client_id"] == "cid-123"

    r = c.get(f"/api/accounts/{acct.id}/credentials/status", headers=_headers(u))
    assert r.json()["enrolled"] is True
    assert "fl-access" not in r.text


def test_oauth_complete_non_default_principal(client, monkeypatch):
    """Tokens land under the account's own principal, keeping credential_ref true."""
    c, Session = client
    db = Session()
    u = _user(db, "oauthprincipal@example.com")
    acct = _account(db, u.id, platform="freelancer", principal="secondary")
    monkeypatch.setenv("FREELANCER_CLIENT_ID", "cid-123")
    monkeypatch.setenv("FREELANCER_CLIENT_SECRET", "sekret")

    async def fake_exchange(self, client_id, client_secret, code, redirect_uri):
        return {"access_token": "tok2", "refresh_token": "ref2",
                "expires_at": "2999-01-01T00:00:00+00:00"}

    monkeypatch.setattr(FreelancerAdapter, "exchange_code", fake_exchange)
    r = c.post(f"/api/accounts/{acct.id}/oauth/freelancer/complete",
               json={"code": "x"}, headers=_headers(u))
    assert r.status_code == 204, r.text
    db.refresh(acct)
    assert acct.credential_ref == "vault://freelancer/secondary"
    assert CredentialVault(db, u.id).load("freelancer", "secondary")["access_token"] == "tok2"


# ---------------- worker stealth-session endpoint ----------------

def test_stealth_session_returns_enrolled_state(client):
    c, Session = client
    db = Session()
    u = _user(db, "session@example.com")
    acct = _account(db, u.id, platform="fiverr")
    r = c.post(f"/api/accounts/{acct.id}/credentials",
               json={"secrets": {"storage_state_json": json.dumps(STORAGE_STATE)}},
               headers=_headers(u))
    assert r.status_code == 204

    _claimed_task(db, u.id)
    r = c.get(f"/api/gigs/stealth-session?platform=fiverr&user_id={u.id}",
              headers=WORKER_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["credentials_present"] is True
    assert body["storage_state"] == STORAGE_STATE

    # the read is audit-logged (never with the session data itself)
    row = (db.query(AuditLog)
           .filter(AuditLog.action_type == "stealth_session_read")
           .one())
    assert row.user_id == u.id and row.platform == "fiverr"
    assert row.detail["worker"] == "worker"
    assert "abc123secret" not in json.dumps(row.detail)


def test_stealth_session_requires_claimed_task(client):
    c, Session = client
    db = Session()
    u = _user(db, "noclaim@example.com")
    acct = _account(db, u.id, platform="fiverr")
    r = c.post(f"/api/accounts/{acct.id}/credentials",
               json={"secrets": {"storage_state_json": json.dumps(STORAGE_STATE)}},
               headers=_headers(u))
    assert r.status_code == 204

    # no active claim → the session stays sealed
    r = c.get(f"/api/gigs/stealth-session?platform=fiverr&user_id={u.id}",
              headers=WORKER_HEADERS)
    assert r.status_code == 409

    # a claim for another platform or tenant does not unseal it
    _claimed_task(db, u.id, platform="upwork")
    r = c.get(f"/api/gigs/stealth-session?platform=fiverr&user_id={u.id}",
              headers=WORKER_HEADERS)
    assert r.status_code == 409


def test_stealth_session_mode_disabled_rejected(client):
    c, Session = client
    db = Session()
    u = _user(db, "killSwitch@example.com")
    acct = _account(db, u.id, platform="fiverr")
    r = c.post(f"/api/accounts/{acct.id}/credentials",
               json={"secrets": {"storage_state_json": json.dumps(STORAGE_STATE)}},
               headers=_headers(u))
    assert r.status_code == 204
    acct.mode = "disabled"
    db.commit()

    # kill switch wins even while a task is claimed
    _claimed_task(db, u.id)
    r = c.get(f"/api/gigs/stealth-session?platform=fiverr&user_id={u.id}",
              headers=WORKER_HEADERS)
    assert r.status_code == 409
    assert "disabled" in r.json()["detail"]


def test_stealth_session_null_when_absent(client):
    c, Session = client
    db = Session()
    u = _user(db, "nosession@example.com")
    _claimed_task(db, u.id)

    r = c.get(f"/api/gigs/stealth-session?platform=fiverr&user_id={u.id}",
              headers=WORKER_HEADERS)
    assert r.status_code == 200
    assert r.json() == {"storage_state": None, "credentials_present": False,
                        "proxy_url": None}

    # account exists but nothing enrolled
    _account(db, u.id, platform="fiverr")
    r = c.get(f"/api/gigs/stealth-session?platform=fiverr&user_id={u.id}",
              headers=WORKER_HEADERS)
    assert r.json() == {"storage_state": None, "credentials_present": False,
                        "proxy_url": None}

    # username/password enrollment: present, but no storage_state to seed from
    acct = db.query(PlatformAccount).filter_by(user_id=u.id, platform="fiverr").one()
    r = c.post(f"/api/accounts/{acct.id}/credentials",
               json={"secrets": {"username": "x", "password": "y"}},
               headers=_headers(u))
    assert r.status_code == 204
    r = c.get(f"/api/gigs/stealth-session?platform=fiverr&user_id={u.id}",
              headers=WORKER_HEADERS)
    assert r.json() == {"storage_state": None, "credentials_present": True,
                        "proxy_url": None}


def test_stealth_session_includes_account_proxy_url(client):
    c, Session = client
    db = Session()
    u = _user(db, "proxy@example.com")
    acct = _account(db, u.id, platform="fiverr")
    r = c.post(f"/api/accounts/{acct.id}/credentials",
               json={"secrets": {"storage_state_json": json.dumps(STORAGE_STATE)}},
               headers=_headers(u))
    assert r.status_code == 204
    acct.settings = {"proxy_url": "http://user:pw@tenant-proxy:9000"}
    db.commit()

    _claimed_task(db, u.id)
    r = c.get(f"/api/gigs/stealth-session?platform=fiverr&user_id={u.id}",
              headers=WORKER_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["proxy_url"] == "http://user:pw@tenant-proxy:9000"
    assert body["storage_state"] == STORAGE_STATE


def test_stealth_session_requires_worker_token(client):
    c, Session = client
    db = Session()
    u = _user(db, "sessionauth@example.com")

    assert c.get(f"/api/gigs/stealth-session?platform=fiverr&user_id={u.id}").status_code == 401
    # a user JWT is not a worker token
    assert c.get(f"/api/gigs/stealth-session?platform=fiverr&user_id={u.id}",
                 headers=_headers(u)).status_code == 401
    assert c.get(f"/api/gigs/stealth-session?platform=fiverr&user_id={u.id}",
                 headers={"Authorization": "Bearer wrong"}).status_code == 401
