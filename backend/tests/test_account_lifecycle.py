"""Account lifecycle: POST /api/auth/password (change) and
DELETE /api/auth/account (password-verified self-deletion).

The deletion test enables SQLite's foreign-key pragma so the ON DELETE
CASCADE behavior every tenant table declares (models + initial migration)
is genuinely exercised, matching Postgres in production.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models import Job, KeywordGroup, User


@pytest.fixture()
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

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


# ---------------- change password ----------------

def test_change_password_flow(client):
    c, _ = client
    token = _register(c, "pw@example.com", password="old-password")["access_token"]

    # wrong current password → 400, hash unchanged
    r = c.post("/api/auth/password", headers=_auth(token),
               json={"current_password": "not-the-password",
                     "new_password": "new-password-1"})
    assert r.status_code == 400
    r = c.post("/api/auth/login", json={"email": "pw@example.com", "password": "old-password"})
    assert r.status_code == 200

    # too-short new password → 422 (min length 8)
    r = c.post("/api/auth/password", headers=_auth(token),
               json={"current_password": "old-password", "new_password": "short"})
    assert r.status_code == 422

    # correct current password → 200; old password stops working, new one logs in
    r = c.post("/api/auth/password", headers=_auth(token),
               json={"current_password": "old-password", "new_password": "new-password-1"})
    assert r.status_code == 200
    r = c.post("/api/auth/login", json={"email": "pw@example.com", "password": "old-password"})
    assert r.status_code == 401
    r = c.post("/api/auth/login", json={"email": "pw@example.com", "password": "new-password-1"})
    assert r.status_code == 200

    # unauthenticated → 401
    r = c.post("/api/auth/password",
               json={"current_password": "x", "new_password": "new-password-1"})
    assert r.status_code == 401


# ---------------- delete account ----------------

def test_delete_account_flow(client):
    c, Session = client
    token = _register(c, "gone@example.com", password="password123")["access_token"]

    # seed some tenant data
    r = c.post("/api/keyword-groups", headers=_auth(token),
               json={"name": "group", "keywords": [{"term": "react", "kind": "primary"}]})
    assert r.status_code == 201
    r = c.post("/api/jobs/ingest", headers=_auth(token), json={"jobs": [
        {"external_id": "j-1", "platform": "upwork", "title": "React app"}]})
    assert r.status_code == 200 and r.json()["ingested"] == 1

    # wrong password → 400, account intact
    r = c.request("DELETE", "/api/auth/account", headers=_auth(token),
                  json={"password": "wrong-password"})
    assert r.status_code == 400
    assert c.get("/api/auth/me", headers=_auth(token)).status_code == 200

    # correct password → account + tenant rows gone
    r = c.request("DELETE", "/api/auth/account", headers=_auth(token),
                  json={"password": "password123"})
    assert r.status_code == 200 and r.json()["status"] == "deleted"

    db = Session()
    try:
        assert db.query(User).filter(User.email == "gone@example.com").count() == 0
        assert db.query(KeywordGroup).count() == 0  # ON DELETE CASCADE
        assert db.query(Job).count() == 0           # ON DELETE CASCADE
    finally:
        db.close()

    # the old token no longer resolves to a user
    assert c.get("/api/auth/me", headers=_auth(token)).status_code == 401
    # login is rejected too
    r = c.post("/api/auth/login", json={"email": "gone@example.com", "password": "password123"})
    assert r.status_code == 401

    # unauthenticated delete → 401
    assert c.request("DELETE", "/api/auth/account",
                     json={"password": "x"}).status_code == 401
