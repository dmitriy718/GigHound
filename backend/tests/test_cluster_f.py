"""Phase 0 Cluster F regression tests.

F-14 digest truth (send_user_digest reports only actually-emailed jobs),
F-15 retry/counter races (generation retry counter after a successful
broker enqueue; fiverr daily offer cap enforced by the atomic INCR;
duplicate buyer-request flush is IntegrityError-safe),
F-17 Redis runtime resilience (cache ops, circuit breaker, LLM bucket,
daily-action budget, and the ingest path all degrade instead of 500ing),
F-18 canonical stealth task kinds + platform validation (register_gig 422,
metrics scrape skips platforms the worker can't serve, emitted task types
resolve in the worker handler registry).
"""
import time

import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import circuit_breaker, fiverr_monitor
from app.auth import hash_password
from app.database import Base, get_db
from app.digest import send_user_digest
from app.fiverr_monitor import (FIVERR_DAILY_OFFER_LIMIT,
                                enqueue_buyer_request_fetch,
                                process_buyer_requests, queue_gig_creation,
                                queue_upwork_catalog_upsert)
from app.gig_analytics import enqueue_metrics_scrape
from app.main import app
from app.models import (AlertSettings, Gig, GigTemplate, Job, PlatformAccount,
                        ProposalQueueItem, StealthTask, User)

# source of truth: worker/handlers/__init__.py HANDLERS (not importable from
# the backend venv — it pulls in playwright). Keep in sync with that file.
WORKER_HANDLER_KEYS = {
    "fetch_buyer_requests", "scrape_gig_metrics", "scrape_competitors",
    "create_gig_draft", "submit_upwork_proposal", "submit_fiverr_offer",
    "submit_proposal", "scrape_proposal_status",
}

GOOD_FIVERR = {
    "title": "I will build a React dashboard",
    "category": "Programming & Tech", "subcategory": "Website Development",
    "tags": ["react", "typescript", "dashboard", "saas", "frontend"],
    "pricing": {"basic": {"price": 50, "delivery_days": 3, "revisions": 1}},
    "description": {"hook": "h", "what_you_get": "w", "why_me": "m", "cta": "c"},
}


class _DeadRedis:
    """A Redis client whose every operation fails — simulates Redis dropping
    AFTER boot (cache._r is set, but the connection is dead)."""

    def __getattr__(self, name):
        def _boom(*a, **kw):
            raise redis.RedisError("redis down")
        return _boom


class _CounterDeadRedis:
    """Reads succeed; counter increments fail — simulates Redis dying between
    the circuit-breaker read and the cap enforcement INCR. (A fully dead
    client is nulled out by app.cache's error handling, which flips every
    consumer to its designed boot-degraded local fallback instead.)"""

    def get(self, key):
        return None

    def set(self, *a, **kw):
        return None

    def incr(self, key):
        raise redis.RedisError("redis down")

    def expire(self, *a, **kw):
        pass


@pytest.fixture(autouse=True)
def force_offline(monkeypatch):
    """Deterministic offline paths: no LLM, no Redis, clean local state."""
    monkeypatch.setattr("app.proposal_gen.llm.llm_available", lambda: False)
    monkeypatch.setattr("app.fiverr_monitor.cache._r", None)
    fiverr_monitor._local_counters.clear()
    monkeypatch.setattr("app.circuit_breaker.cache._r", None)
    circuit_breaker._local.clear()


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def user(db):
    u = User(email="clusterf@example.com",
             password_hash=hash_password("password123"), display_name="CF")
    db.add(u)
    db.commit()
    return u


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


def _register(client, email="clusterf-api@example.com"):
    r = client.post("/api/auth/register",
                    json={"email": email, "password": "password123", "display_name": "CF"})
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _fiverr_template(db, user_id):
    tpl = GigTemplate(user_id=user_id, platform="fiverr", name="React Gig",
                      template_json=GOOD_FIVERR, is_active=True)
    db.add(tpl)
    db.commit()
    return tpl


def _fiverr_account(db, user_id, username="seller1"):
    acct = PlatformAccount(user_id=user_id, platform="fiverr", label="f",
                           enabled=True, mode="stealth",
                           settings={"username": username})
    db.add(acct)
    db.commit()
    return acct


MATCHING_REQUEST = {"id": "br-f1", "title": "Need a React website developer",
                    "budget": 120, "description": "react dashboard work"}


# ---------------- F-14: digest truth ----------------

def test_digest_sent_count_reflects_actual_send(db, user, monkeypatch):
    db.add(AlertSettings(user_id=user.id, digest_mode="hourly", min_score_alert=50.0))
    db.add(Job(user_id=user.id, external_id="dg-f", platform="upwork", title="hot",
               status="new", quality_score=80.0))
    db.commit()

    # SMTP not configured → send_digest_email False → nothing was "sent"
    monkeypatch.setattr("app.digest.send_digest_email", lambda jobs, mode: False)
    assert send_user_digest(db, user.id) == 0
    # actually emailed → the job count is reported
    monkeypatch.setattr("app.digest.send_digest_email", lambda jobs, mode: True)
    assert send_user_digest(db, user.id) == 1


# ---------------- F-15: retry/counter races ----------------

def test_generation_retry_broker_down_keeps_counter(db, user, monkeypatch):
    Session = sessionmaker(bind=db.get_bind())
    monkeypatch.setattr("app.tasks.SessionLocal", Session)

    def _broker_down(job_id):
        raise ConnectionError("broker down")

    monkeypatch.setattr("app.tasks.generate_proposal_task.delay", _broker_down)

    job = Job(user_id=user.id, external_id="rt-f", platform="upwork", title="J")
    db.add(job)
    db.commit()
    item = ProposalQueueItem(user_id=user.id, job_id=job.id, platform="upwork",
                             status="generation_failed", submission_result={})
    db.add(item)
    db.commit()

    from app.tasks import generation_retry_tick_core
    assert generation_retry_tick_core() == {"retried": []}
    db.refresh(item)
    # the failed enqueue must not consume one of the retry attempts
    assert not (item.submission_result or {}).get("generation_retries")


def test_offer_cap_enforced_by_atomic_counter(db, user):
    _fiverr_template(db, user.id)
    # daily counter already at the platform cap → the atomic INCR gate trips
    key = fiverr_monitor._offers_key(user.id)
    fiverr_monitor._local_counters[key] = (FIVERR_DAILY_OFFER_LIMIT,
                                           time.time() + 3600)
    result = process_buyer_requests(db, user.id, [MATCHING_REQUEST])
    assert result["queued"] == 0
    assert db.query(ProposalQueueItem).count() == 0
    assert db.query(Job).count() == 0


def test_duplicate_buyer_request_flush_is_safe(db, user, monkeypatch):
    """A concurrent monitor run posting the same request after the exists
    check hits the unique constraint — caught, not an unhandled error."""
    _fiverr_template(db, user.id)
    real_flush = db.flush

    def _racy_flush(*a, **kw):
        # only the explicit flush with a new Job pending races; query-invoked
        # autoflush earlier in the loop must pass through
        if any(isinstance(o, Job) for o in db.new):
            raise IntegrityError("INSERT INTO jobs", {},
                                 Exception("UNIQUE constraint failed"))
        return real_flush(*a, **kw)

    monkeypatch.setattr(db, "flush", _racy_flush)
    result = process_buyer_requests(db, user.id, [MATCHING_REQUEST])
    assert result["queued"] == 0


# ---------------- F-17: Redis runtime resilience ----------------

def test_ingest_succeeds_when_redis_dies(client, monkeypatch):
    c, Session = client
    token = _register(c)
    monkeypatch.setattr("app.routers.jobs.cache._r", _DeadRedis())
    monkeypatch.setattr("app.ingest.cache._r", _DeadRedis())
    r = c.post("/api/jobs/ingest", headers=_auth(token), json={"jobs": [
        {"external_id": "rd-1", "platform": "upwork", "title": "React app",
         "description": "react work"}]})
    assert r.status_code == 200, r.text
    assert r.json()["ingested"] == 1


def test_circuit_breaker_degrades_to_local_when_redis_dies(monkeypatch):
    monkeypatch.setattr("app.circuit_breaker.cache._r", _DeadRedis())
    assert circuit_breaker.check("fiverr", 1) == (True, "")
    # local fallback still honors state transitions (writes don't raise either)
    circuit_breaker.open_circuit("fiverr", "manual")
    allowed, reason = circuit_breaker.check("fiverr")
    assert not allowed and "circuit OPEN" in reason
    circuit_breaker.close_circuit("fiverr")
    assert circuit_breaker.check("fiverr") == (True, "")


def test_daily_action_budget_noops_when_redis_dies(monkeypatch):
    from app.adapters.ratelimit import consume_daily_action
    monkeypatch.setenv("GIGHOUND_DAILY_CAP_UPWORK", "5")
    monkeypatch.setattr("app.adapters.ratelimit.cache._r", _DeadRedis())
    assert consume_daily_action("upwork", "principal") == 0  # graceful no-op


def test_llm_token_bucket_fails_open_when_redis_dies(monkeypatch):
    from app.textgen import _bucket
    monkeypatch.setattr("app.textgen.cache._r", _DeadRedis())
    assert _bucket.try_acquire(1) is True


def test_offer_cycle_skipped_when_counter_unavailable(db, user, monkeypatch):
    """Redis down mid-run → the offer cap can't be enforced → skip the cycle
    conservatively (never send offers), without raising."""
    _fiverr_template(db, user.id)
    monkeypatch.setattr("app.fiverr_monitor.cache._r", _CounterDeadRedis())
    result = process_buyer_requests(db, user.id, [MATCHING_REQUEST])
    assert result["queued"] == 0
    assert db.query(Job).count() == 0
    assert db.query(ProposalQueueItem).count() == 0


def test_gig_creation_refused_when_counter_unavailable(db, user, monkeypatch):
    tpl = _fiverr_template(db, user.id)
    monkeypatch.setattr("app.fiverr_monitor.cache._r", _CounterDeadRedis())
    task, err = queue_gig_creation(db, tpl)
    assert task is None and "Redis down" in err
    assert db.query(StealthTask).count() == 0


# ---------------- F-18: task kinds + platform validation ----------------

def test_register_gig_validates_platform(client):
    c, Session = client
    token = _register(c)
    r = c.post("/api/gigs", headers=_auth(token),
               json={"platform": "myspace", "title": "x"})
    assert r.status_code == 422
    # missing platform → 422, not a KeyError 500
    assert c.post("/api/gigs", headers=_auth(token),
                  json={"title": "x"}).status_code == 422
    # a schema-supported platform still registers
    r = c.post("/api/gigs", headers=_auth(token),
               json={"platform": "fiverr", "title": "x"})
    assert r.status_code == 201, r.text


def test_metrics_scrape_skips_platforms_worker_cant_serve(db, user):
    db.add(Gig(user_id=user.id, platform="linkedin", title="g", url="https://x/l"))
    db.add(Gig(user_id=user.id, platform="fiverr", title="g2", url="https://x/f"))
    db.commit()
    tasks = enqueue_metrics_scrape(db, user.id)
    assert [t.platform for t in tasks] == ["fiverr"]
    # no permanent-pending task minted for the unservable platform
    assert db.query(StealthTask).filter_by(platform="linkedin").count() == 0


def test_emitted_task_types_resolve_in_worker_registry(db, user):
    # create-gig draft (fiverr only)
    tpl = _fiverr_template(db, user.id)
    task, err = queue_gig_creation(db, tpl)
    assert err == "" and task.task_type == "create_gig_draft"
    assert task.task_type in WORKER_HANDLER_KEYS

    # non-fiverr platforms log + skip instead of emitting an unhandled kind
    other = GigTemplate(user_id=user.id, platform="peopleperhour", name="t",
                        template_json={}, is_active=True)
    db.add(other)
    db.commit()
    task2, err2 = queue_gig_creation(db, other)
    assert task2 is None and "not supported" in err2

    # upwork catalog upsert emits nothing the worker would 100%-fail on
    task3, err3 = queue_upwork_catalog_upsert(db, other)
    assert task3 is None and err3
    assert db.query(StealthTask).filter_by(task_type="upwork_catalog_upsert").count() == 0

    # buyer-request fetch
    _fiverr_account(db, user.id)
    fetch = enqueue_buyer_request_fetch(db, user.id)
    assert fetch.task_type == "fetch_buyer_requests"
    assert fetch.task_type in WORKER_HANDLER_KEYS

    # metrics scrape
    db.add(Gig(user_id=user.id, platform="fiverr", title="g", url="https://x/g"))
    db.commit()
    scrapes = enqueue_metrics_scrape(db, user.id)
    assert scrapes and all(t.task_type == "scrape_gig_metrics" for t in scrapes)
    assert all(t.task_type in WORKER_HANDLER_KEYS for t in scrapes)

    # nothing legacy was emitted anywhere
    emitted = {t.task_type for t in db.query(StealthTask).all()}
    assert emitted <= WORKER_HANDLER_KEYS
