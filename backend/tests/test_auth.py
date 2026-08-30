"""Tests for auth (bcrypt hashing, JWT, register/login/me) and per-tenant
data isolation across keyword groups, jobs, proposals, and the alerts WS."""
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.websockets import WebSocketDisconnect

from app.auth import (create_access_token, decode_token, hash_password,
                      verify_password)
from app.config import SECRET_KEY
from app.database import Base, get_db
from app.main import app


@pytest.fixture(autouse=True)
def force_offline(monkeypatch):
    """Deterministic offline paths: no LLM, no Redis rate-limit bucket."""
    monkeypatch.setattr("app.proposal_gen.llm.llm_available", lambda: False)
    monkeypatch.setattr("app.routers.auth.cache._r", None)


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


def _register(client, email, password="password123", name="Test User"):
    r = client.post("/api/auth/register",
                    json={"email": email, "password": password, "display_name": name})
    assert r.status_code == 201, r.text
    return r.json()


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


# ---------------- hashing & JWT ----------------

def test_password_hash_roundtrip():
    h = hash_password("correct horse battery staple")
    assert h != "correct horse battery staple"
    assert verify_password("correct horse battery staple", h) is True
    assert verify_password("wrong", h) is False
    assert verify_password("anything", "not-a-bcrypt-hash") is False


class _FakeUser:
    id = 42
    email = "jwt@example.com"


def test_jwt_roundtrip():
    token = create_access_token(_FakeUser())
    payload = decode_token(token)
    assert payload["sub"] == "42"
    assert payload["email"] == "jwt@example.com"
    exp = datetime.fromtimestamp(payload["exp"], timezone.utc)
    assert timedelta(hours=11) < exp - datetime.now(timezone.utc) <= timedelta(hours=12)


def test_jwt_expired_rejected():
    past = datetime.now(timezone.utc) - timedelta(hours=13)
    token = jwt.encode({"sub": "42", "email": "jwt@example.com",
                        "exp": past + timedelta(hours=12)},
                       SECRET_KEY, algorithm="HS256")
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_token(token)


def test_jwt_wrong_secret_rejected():
    token = jwt.encode({"sub": "42", "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
                       "some-other-secret", algorithm="HS256")
    with pytest.raises(jwt.InvalidSignatureError):
        decode_token(token)


# ---------------- register / login / me ----------------

def test_register_login_me_flow(client):
    c, _ = client
    data = _register(c, "  Alice@Example.COM ", name="Alice")
    assert data["token_type"] == "bearer"
    assert data["user"]["email"] == "alice@example.com"  # lowercased
    assert "password" not in data["user"] and "password_hash" not in data["user"]

    # duplicate registration rejected
    r = c.post("/api/auth/register", json={
        "email": "alice@example.com", "password": "password123", "display_name": "A2"})
    assert r.status_code == 409

    # login with mixed-case email works; wrong password does not
    r = c.post("/api/auth/login", json={"email": "Alice@example.com", "password": "password123"})
    assert r.status_code == 200
    token = r.json()["access_token"]
    r = c.post("/api/auth/login", json={"email": "alice@example.com", "password": "nope-nope"})
    assert r.status_code == 401

    r = c.get("/api/auth/me", headers=_auth(token))
    assert r.status_code == 200 and r.json()["email"] == "alice@example.com"
    # no token → 401 (auth is enabled: GIGHOUND_SECRET_KEY is set, DEV_NOAUTH off)
    assert c.get("/api/auth/me").status_code == 401
    assert c.get("/api/auth/me", headers=_auth("garbage.token.here")).status_code == 401


def test_register_rejects_short_password(client):
    c, _ = client
    r = c.post("/api/auth/register",
               json={"email": "short@example.com", "password": "x" * 7, "display_name": "S"})
    assert r.status_code == 422


# ---------------- per-tenant isolation ----------------

def test_per_user_isolation(client, monkeypatch):
    c, Session = client
    alice = _register(c, "alice@example.com")["access_token"]
    bob = _register(c, "bob@example.com")["access_token"]

    # --- keyword groups ---
    r = c.post("/api/keyword-groups", headers=_auth(alice),
               json={"name": "Alice group", "keywords": [{"term": "react", "kind": "primary"}]})
    assert r.status_code == 201
    group_id = r.json()["id"]
    assert [g["name"] for g in c.get("/api/keyword-groups", headers=_auth(alice)).json()] == ["Alice group"]
    assert c.get("/api/keyword-groups", headers=_auth(bob)).json() == []
    # Bob cannot mutate Alice's group
    assert c.put(f"/api/keyword-groups/{group_id}", headers=_auth(bob),
                 json={"name": "hijacked"}).status_code == 404
    assert c.delete(f"/api/keyword-groups/{group_id}", headers=_auth(bob)).status_code == 404

    # --- jobs ---
    r = c.post("/api/jobs/ingest", headers=_auth(alice), json={"jobs": [
        {"external_id": "j-1", "platform": "upwork", "title": "React dashboard build",
         "description": "react typescript work"}]})
    assert r.status_code == 200 and r.json()["ingested"] == 1
    assert c.get("/api/jobs", headers=_auth(alice)).json()["total"] == 1
    assert c.get("/api/jobs", headers=_auth(bob)).json()["total"] == 0
    job_id = c.get("/api/jobs", headers=_auth(alice)).json()["jobs"][0]["id"]
    assert c.get(f"/api/jobs/{job_id}", headers=_auth(bob)).status_code == 404
    assert c.post(f"/api/jobs/{job_id}/archive", headers=_auth(bob)).status_code == 404
    # the same external job ingested by Bob is a separate tenant row, not a dup
    r = c.post("/api/jobs/ingest", headers=_auth(bob), json={"jobs": [
        {"external_id": "j-1", "platform": "upwork", "title": "React dashboard build"}]})
    assert r.json()["ingested"] == 1

    # --- proposals (ingest enqueues generation; run the task core directly) ---
    from app.tasks import generate_proposal_core
    monkeypatch.setattr("app.tasks.SessionLocal", Session)
    generate_proposal_core(job_id)
    bob_job_id = c.get("/api/jobs", headers=_auth(bob)).json()["jobs"][0]["id"]
    generate_proposal_core(bob_job_id)
    alice_props = c.get("/api/proposals", headers=_auth(alice)).json()["items"]
    bob_props = c.get("/api/proposals", headers=_auth(bob)).json()["items"]
    assert len(alice_props) == 1 and len(bob_props) == 1
    assert alice_props[0]["id"] != bob_props[0]["id"]
    prop_id = alice_props[0]["id"]
    assert c.get(f"/api/proposals/{prop_id}", headers=_auth(bob)).status_code == 404
    assert c.post(f"/api/proposals/{prop_id}/approve", headers=_auth(bob),
                  json={"reviewer": "bob"}).status_code == 404

    # --- alert settings are per-user ---
    assert c.put("/api/alerts/settings", headers=_auth(alice),
                 json={"min_score_alert": 55.0}).status_code == 200
    assert c.get("/api/alerts/settings", headers=_auth(alice)).json()["min_score_alert"] == 55.0
    assert c.get("/api/alerts/settings", headers=_auth(bob)).json()["min_score_alert"] == 70.0

    # --- unauthenticated requests are rejected everywhere ---
    assert c.get("/api/jobs").status_code == 401
    assert c.get("/api/keyword-groups").status_code == 401
    assert c.get("/api/proposals").status_code == 401


# ---------------- WebSocket auth ----------------

def test_ws_rejects_missing_or_bad_token(client):
    c, _ = client
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws/alerts"):
            pass
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws/alerts?token=garbage"):
            pass


def test_ws_accepts_valid_token(client):
    c, _ = client
    token = _register(c, "ws@example.com")["access_token"]
    with c.websocket_connect(f"/ws/alerts?token={token}") as ws:
        ws.send_text("ping")  # server keeps the socket open for pings
