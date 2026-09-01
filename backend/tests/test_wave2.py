"""Final-wave tests: Upwork proposal-status sync via the stealth worker,
follow-up due automation, the analytics trend endpoint, and the bid_advice
refresh in GET /api/proposals."""
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
from app.models import (AuditLog, Job, PlatformAccount, ProposalQueueItem,
                        StealthTask, Template, User)

WORKER_HEADERS = {"Authorization": "Bearer test-worker-token"}
NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def force_offline(monkeypatch):
    """Deterministic offline paths: no LLM, no Redis circuit/cache buckets."""
    monkeypatch.setattr("app.circuit_breaker.cache._r", None)
    circuit_breaker._local.clear()
    monkeypatch.setattr("app.proposal_gen.llm.llm_available", lambda: False)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def user(db):
    u = User(email="wave2@example.com",
             password_hash=hash_password("password123"), display_name="W2")
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


def _job(db, user_id, platform="upwork", external_id="~abc123", **kw):
    job = Job(user_id=user_id, platform=platform, external_id=external_id,
              title="React dashboard build", description="react typescript",
              url=f"https://www.upwork.com/jobs/{external_id}",
              proposals_count=kw.pop("proposals_count", 3),
              quality_score=kw.pop("quality_score", 80.0), **kw)
    db.add(job)
    db.commit()
    return job


def _item(db, user_id, job, status="submitted", outcome="pending",
          reviewed_at=None, created_at=None, request_type="job",
          client_replied_at=None, submission_result=None, bid_advice=None):
    item = ProposalQueueItem(
        user_id=user_id, job_id=job.id, platform=job.platform,
        proposal_text="hi", status=status, outcome=outcome,
        reviewed_at=reviewed_at, request_type=request_type,
        client_replied_at=client_replied_at,
        submission_result=submission_result or {}, bid_advice=bid_advice)
    if created_at is not None:
        item.created_at = created_at
    db.add(item)
    db.commit()
    return item


# ---------------- 1. Upwork outcome sync via worker ----------------

def test_upwork_outcome_tick_enqueues_scrape_task(db, user, monkeypatch):
    from app.tasks import upwork_outcome_user_core
    monkeypatch.setattr("app.tasks.SessionLocal", sessionmaker(bind=db.bind))

    db.add(PlatformAccount(user_id=user.id, platform="upwork", label="uw",
                           enabled=True))
    db.commit()
    job = _job(db, user.id)
    open_item = _item(db, user.id, job, status="submitted")
    browser_item = _item(db, user.id, job, status="queued_for_browser")
    _item(db, user.id, job, status="pending_review")  # not watched

    result = upwork_outcome_user_core(user.id)
    assert result["enqueued"] == 1

    task = db.get(StealthTask, result["task_ids"][0])
    assert task.task_type == "scrape_proposal_status"
    assert task.platform == "upwork" and task.user_id == user.id
    checks = {c["proposal_queue_item_id"]: c for c in task.payload["items"]}
    assert set(checks) == {open_item.id, browser_item.id}
    assert checks[open_item.id]["job_external_id"] == "~abc123"
    assert checks[open_item.id]["job_url"].endswith("~abc123")

    # a task already in flight → no duplicate stacking
    assert upwork_outcome_user_core(user.id)["enqueued"] == 0


def test_upwork_outcome_tick_circuit_open_not_counted(db, user, monkeypatch):
    """A scrape task skipped by the circuit breaker is recorded for UI
    visibility but must not count as enqueued work (it will never run)."""
    from app.tasks import upwork_outcome_user_core
    monkeypatch.setattr("app.tasks.SessionLocal", sessionmaker(bind=db.bind))

    db.add(PlatformAccount(user_id=user.id, platform="upwork", label="uw",
                           enabled=True))
    db.commit()
    job = _job(db, user.id)
    _item(db, user.id, job, status="submitted")

    circuit_breaker.open_circuit("upwork", "manual halt")
    result = upwork_outcome_user_core(user.id)
    assert result["enqueued"] == 0 and result["task_ids"] == []
    task = db.query(StealthTask).filter(
        StealthTask.task_type == "scrape_proposal_status").one()
    assert task.status == "skipped_circuit_open"
    assert "manual halt" in task.result["reason"]


def test_upwork_outcome_tick_skips_without_account_or_items(db, user, monkeypatch):
    from app.tasks import upwork_outcome_user_core
    monkeypatch.setattr("app.tasks.SessionLocal", sessionmaker(bind=db.bind))

    # enabled account but no open proposals → nothing to check
    db.add(PlatformAccount(user_id=user.id, platform="upwork", label="uw",
                           enabled=True))
    db.commit()
    assert upwork_outcome_user_core(user.id)["enqueued"] == 0

    # open proposals but no enabled upwork account → nothing to check
    other = User(email="wave2b@example.com",
                 password_hash=hash_password("password123"))
    db.add(other)
    db.commit()
    job = _job(db, other.id)
    _item(db, other.id, job, status="submitted")
    assert upwork_outcome_user_core(other.id)["enqueued"] == 0


def test_platform_outcome_tick_covers_fiverr_pph_guru(db, user, monkeypatch):
    """The generalized tick enqueues one scrape task per browser platform
    with an enabled account + open items (not just upwork)."""
    from app.tasks import upwork_outcome_user_core
    monkeypatch.setattr("app.tasks.SessionLocal", sessionmaker(bind=db.bind))

    for platform in ("fiverr", "peopleperhour", "guru"):
        db.add(PlatformAccount(user_id=user.id, platform=platform,
                               label=platform, enabled=True))
        job = _job(db, user.id, platform=platform,
                   external_id=f"{platform}-1")
        _item(db, user.id, job, status="submitted")
    # guru account disabled → no task for guru
    db.query(PlatformAccount) \
        .filter(PlatformAccount.user_id == user.id,
                PlatformAccount.platform == "guru") \
        .update({"enabled": False})
    # freelancer is an API platform — never browser-scraped
    db.add(PlatformAccount(user_id=user.id, platform="freelancer",
                           label="fl", enabled=True))
    fl_job = _job(db, user.id, platform="freelancer", external_id="fl-1")
    _item(db, user.id, fl_job, status="submitted")
    db.commit()

    result = upwork_outcome_user_core(user.id)
    assert result["enqueued"] == 2
    tasks = [db.get(StealthTask, tid) for tid in result["task_ids"]]
    assert {t.platform for t in tasks} == {"fiverr", "peopleperhour"}
    for t in tasks:
        assert t.task_type == "scrape_proposal_status"
        assert t.user_id == user.id
        assert len(t.payload["items"]) == 1

    # per-platform in-flight dedupe: nothing new until those complete
    assert upwork_outcome_user_core(user.id)["enqueued"] == 0


def _scrape_task(db, user_id, status="claimed", platform="upwork"):
    task = StealthTask(user_id=user_id, platform=platform,
                       task_type="scrape_proposal_status",
                       payload={"items": []}, status=status)
    db.add(task)
    db.commit()
    return task


def test_proposal_status_endpoint_maps_and_completes(client, monkeypatch):
    c, Session = client
    db = Session()
    u = User(email="ps@example.com", password_hash=hash_password("password123"))
    db.add(u)
    db.commit()
    job = _job(db, u.id)
    hired_item = _item(db, u.id, job)
    declined_item = _item(db, u.id, job)
    reply_item = _item(db, u.id, job)
    quiet_item = _item(db, u.id, job)
    # template win-rate learning must not double-count on reposts
    tpl = Template(user_id=u.id, title="t", platform="upwork",
                   source_proposal_id=hired_item.id)
    db.add(tpl)
    db.commit()
    task = _scrape_task(db, u.id)

    sent = []

    async def _capture(user_id, message):
        sent.append(message)

    monkeypatch.setattr("app.proposal_status_sync.alerts.broadcast", _capture)

    results = [
        {"proposal_queue_item_id": hired_item.id, "platform_status": "hired",
         "has_unread_reply": False},
        {"proposal_queue_item_id": declined_item.id, "platform_status": "declined",
         "has_unread_reply": False},
        {"proposal_queue_item_id": reply_item.id, "platform_status": "interviewing",
         "has_unread_reply": True},
        {"proposal_queue_item_id": quiet_item.id, "platform_status": "viewed",
         "has_unread_reply": False},
    ]
    r = c.post("/api/gigs/proposal-status",
               json={"task_id": task.id, "results": results},
               headers=WORKER_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcomes"] == 2 and body["replies"] == 1
    assert body["task_status"] == "done"

    for item, outcome in ((hired_item, "hired"), (declined_item, "rejected"),
                          (reply_item, "pending"), (quiet_item, "pending")):
        db.refresh(item)
        assert item.outcome == outcome
    assert reply_item.client_replied_at is not None
    assert quiet_item.client_replied_at is None
    db.refresh(tpl)
    assert tpl.wins == 1 and tpl.losses == 0
    db.refresh(task)
    assert task.status == "done" and task.completed_at is not None
    assert len(sent) == 1
    assert sent[0]["type"] == "client_replied"
    assert sent[0]["proposal_id"] == reply_item.id

    # idempotent repost: nothing re-applied, no duplicate broadcast/wins
    r = c.post("/api/gigs/proposal-status",
               json={"task_id": task.id, "results": results},
               headers=WORKER_HEADERS)
    assert r.status_code == 200
    assert r.json()["outcomes"] == 0 and r.json()["replies"] == 0
    db.refresh(tpl)
    assert tpl.wins == 1
    assert len(sent) == 1


def test_proposal_status_applies_for_new_browser_platforms(client, monkeypatch):
    """Result application is platform-agnostic: a fiverr scrape task applies
    outcomes/replies to fiverr items with the same tenancy + idempotency."""
    c, Session = client
    db = Session()
    u = User(email="psf@example.com", password_hash=hash_password("password123"))
    other = User(email="psf2@example.com",
                 password_hash=hash_password("password123"))
    db.add_all([u, other])
    db.commit()
    job = _job(db, u.id, platform="fiverr", external_id="brief-9")
    hired_item = _item(db, u.id, job)
    reply_item = _item(db, u.id, job)
    foreign = _item(db, other.id,
                    _job(db, other.id, platform="fiverr", external_id="zz"))
    task = _scrape_task(db, u.id, platform="fiverr")

    sent = []

    async def _capture(user_id, message):
        sent.append(message)

    monkeypatch.setattr("app.proposal_status_sync.alerts.broadcast", _capture)

    results = [
        {"proposal_queue_item_id": hired_item.id, "platform_status": "hired",
         "has_unread_reply": False},
        {"proposal_queue_item_id": reply_item.id, "platform_status": "pending",
         "has_unread_reply": True},
        {"proposal_queue_item_id": foreign.id, "platform_status": "hired",
         "has_unread_reply": True},
    ]
    r = c.post("/api/gigs/proposal-status",
               json={"task_id": task.id, "results": results},
               headers=WORKER_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcomes"] == 1 and body["replies"] == 1
    assert body["skipped"] == 1  # cross-tenant row rejected
    db.refresh(hired_item)
    db.refresh(reply_item)
    db.refresh(foreign)
    assert hired_item.outcome == "hired"
    assert reply_item.client_replied_at is not None
    assert foreign.outcome == "pending" and foreign.client_replied_at is None
    assert len(sent) == 1

    # idempotent repost
    r = c.post("/api/gigs/proposal-status",
               json={"task_id": task.id, "results": results},
               headers=WORKER_HEADERS)
    assert r.json()["outcomes"] == 0 and r.json()["replies"] == 0
    assert len(sent) == 1


def test_proposal_status_endpoint_guards(client):
    c, Session = client
    db = Session()
    u = User(email="psg@example.com", password_hash=hash_password("password123"))
    other = User(email="psg2@example.com",
                 password_hash=hash_password("password123"))
    db.add_all([u, other])
    db.commit()
    job = _job(db, u.id)
    foreign = _item(db, other.id, _job(db, other.id, external_id="~zzz"))
    task = _scrape_task(db, u.id)

    # auth: no token / user JWT → 401
    assert c.post("/api/gigs/proposal-status",
                  json={"task_id": task.id, "results": []}).status_code == 401
    user_headers = {"Authorization": f"Bearer {create_access_token(u)}"}
    assert c.post("/api/gigs/proposal-status",
                  json={"task_id": task.id, "results": []},
                  headers=user_headers).status_code == 401

    # validation + lookup
    assert c.post("/api/gigs/proposal-status", json={"task_id": task.id},
                  headers=WORKER_HEADERS).status_code == 422
    assert c.post("/api/gigs/proposal-status",
                  json={"task_id": 99999, "results": []},
                  headers=WORKER_HEADERS).status_code == 404
    wrong_kind = StealthTask(user_id=u.id, platform="upwork",
                             task_type="submit_upwork_proposal", payload={})
    db.add(wrong_kind)
    db.commit()
    assert c.post("/api/gigs/proposal-status",
                  json={"task_id": wrong_kind.id, "results": []},
                  headers=WORKER_HEADERS).status_code == 404

    # cross-tenant results and unknown statuses are skipped, not applied
    item = _item(db, u.id, job)
    r = c.post("/api/gigs/proposal-status",
               json={"task_id": task.id, "results": [
                   {"proposal_queue_item_id": foreign.id,
                    "platform_status": "hired", "has_unread_reply": True},
                   {"proposal_queue_item_id": item.id,
                    "platform_status": "bogus", "has_unread_reply": False},
               ]},
               headers=WORKER_HEADERS)
    assert r.status_code == 200
    assert r.json()["skipped"] == 2
    db.refresh(foreign)
    assert foreign.outcome == "pending" and foreign.client_replied_at is None


# ---------------- 2. follow-up due automation ----------------

def test_follow_up_due_gating(db, user, monkeypatch):
    from app.tasks import follow_up_due_user_core
    monkeypatch.setattr("app.tasks.SessionLocal", sessionmaker(bind=db.bind))
    sent = []

    async def _capture(user_id, message):
        sent.append(message)

    monkeypatch.setattr("app.follow_up.alerts.broadcast", _capture)

    job = _job(db, user.id)
    old = NOW - timedelta(days=6)
    eligible = _item(db, user.id, job, reviewed_at=old)
    _item(db, user.id, job, reviewed_at=NOW - timedelta(days=2))  # too recent
    _item(db, user.id, job, status="queued_for_browser",
          reviewed_at=old)  # not confirmed submitted yet
    _item(db, user.id, job, outcome="hired", reviewed_at=old)  # terminal
    _item(db, user.id, job, reviewed_at=old,
          client_replied_at=NOW)  # client already replied
    has_child = _item(db, user.id, job, reviewed_at=old)
    _item(db, user.id, job, status="rejected", request_type="follow_up",
          submission_result={"parent_proposal_id": has_child.id})

    result = follow_up_due_user_core(user.id)
    assert len(result["queued"]) == 1

    follow = db.get(ProposalQueueItem, result["queued"][0])
    assert follow.request_type == "follow_up"
    assert follow.status == "pending_review"
    assert follow.submission_result["parent_proposal_id"] == eligible.id
    assert follow.submission_result["auto"] is True
    assert follow.proposal_text  # offline composer produced text

    audit = (db.query(AuditLog)
             .filter(AuditLog.action_type == "follow_up_generated")
             .one())
    assert audit.detail["auto"] is True
    assert audit.detail["parent_proposal_id"] == eligible.id

    assert len(sent) == 1
    assert sent[0]["type"] == "proposal_queued"
    assert sent[0]["proposal_id"] == follow.id

    # re-run: the child now exists → nothing new (no daily nagging)
    assert follow_up_due_user_core(user.id)["queued"] == []


def test_follow_up_due_cap_per_run(db, user, monkeypatch):
    from app.follow_up import FOLLOW_UP_CAP_PER_RUN
    from app.tasks import follow_up_due_user_core
    monkeypatch.setattr("app.tasks.SessionLocal", sessionmaker(bind=db.bind))

    async def _noop(user_id, message):
        return None

    monkeypatch.setattr("app.follow_up.alerts.broadcast", _noop)

    old = NOW - timedelta(days=6)
    for i in range(FOLLOW_UP_CAP_PER_RUN + 2):
        job = _job(db, user.id, external_id=f"~cap{i}")
        _item(db, user.id, job, reviewed_at=old)

    result = follow_up_due_user_core(user.id)
    assert len(result["queued"]) == FOLLOW_UP_CAP_PER_RUN


# ---------------- 3. analytics trend ----------------

def test_analytics_trend(client):
    c, Session = client
    db = Session()
    u = User(email="trend@example.com",
             password_hash=hash_password("password123"))
    db.add(u)
    db.commit()
    job = _job(db, u.id, platform="freelancer")

    replied = _item(db, u.id, job, reviewed_at=NOW, client_replied_at=NOW)
    hired = _item(db, u.id, job, reviewed_at=NOW)
    from app.templates import record_outcome
    record_outcome(db, hired, "hired")  # stamps outcome_recorded_at
    two_weeks_ago = NOW - timedelta(days=14)
    _item(db, u.id, job, reviewed_at=two_weeks_ago)
    # another tenant's data must not leak in
    v = User(email="trend2@example.com",
             password_hash=hash_password("password123"))
    db.add(v)
    db.commit()
    _item(db, v.id, _job(db, v.id, platform="freelancer", external_id="99"),
          reviewed_at=NOW, client_replied_at=NOW)

    headers = {"Authorization": f"Bearer {create_access_token(u)}"}
    r = c.get("/api/analytics/trend?weeks=8", headers=headers)
    assert r.status_code == 200, r.text
    weeks = r.json()["weeks"]
    assert len(weeks) == 8
    assert [w["week"] for w in weeks] == sorted(w["week"] for w in weeks)  # oldest first

    current_label = NOW.strftime("%G-W%V")
    assert weeks[-1]["week"] == current_label
    current = weeks[-1]
    assert current["submitted"] == 2
    assert current["replied"] == 1
    assert current["hired"] == 1
    assert current["win_rate"] == 100.0

    old_label = two_weeks_ago.strftime("%G-W%V")
    old_week = next(w for w in weeks if w["week"] == old_label)
    assert old_week["submitted"] == 1
    assert old_week["hired"] == 0

    empty = next(w for w in weeks if w["week"] not in (current_label, old_label))
    assert empty == {"week": empty["week"], "submitted": 0, "replied": 0,
                     "hired": 0, "win_rate": None}

    # weeks param bounds
    assert c.get("/api/analytics/trend?weeks=0", headers=headers).status_code == 422
    assert c.get("/api/analytics/trend?weeks=27", headers=headers).status_code == 422
    assert len(c.get("/api/analytics/trend?weeks=1", headers=headers)
               .json()["weeks"]) == 1
    # auth required
    assert c.get("/api/analytics/trend").status_code == 401


# ---------------- 4. bid_advice refresh ----------------

def test_bid_advice_refreshed_for_stale_items(client):
    c, Session = client
    db = Session()
    u = User(email="advice@example.com",
             password_hash=hash_password("password123"))
    db.add(u)
    db.commit()
    # hot market + low score → the fresh advice is 'skip'
    job = _job(db, u.id, proposals_count=30, quality_score=50.0)
    stale_advice = {"recommendation": "bid", "reason": "computed long ago"}
    old_item = _item(db, u.id, job, status="pending_review",
                     created_at=NOW - timedelta(days=2), bid_advice=stale_advice)
    fresh_item = _item(db, u.id, job, status="pending_review",
                       created_at=NOW, bid_advice=stale_advice)

    headers = {"Authorization": f"Bearer {create_access_token(u)}"}
    r = c.get("/api/proposals", headers=headers)
    assert r.status_code == 200, r.text
    by_id = {i["id"]: i for i in r.json()["items"]}
    assert by_id[old_item.id]["bid_advice"]["recommendation"] == "skip"
    # fresh items are NOT recomputed (the refresh is age-gated)
    assert by_id[fresh_item.id]["bid_advice"] == stale_advice

    # persisted, not just response-local
    db.refresh(old_item)
    assert old_item.bid_advice["recommendation"] == "skip"
