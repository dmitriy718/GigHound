"""Phase 2 tests: scheduled discovery, generation off the request path,
prompt_hints wiring, outcome/reply sync, template provenance, safe
automations (digest/retry/auto-archive), bulk archive, dedupe hardening,
analytics funnel, and the Redis WS fan-out."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import proposal_gen
from app.adapters.schema import JobPosting
from app.auth import hash_password
from app.database import Base, get_db
from app.main import app
from app.models import (AdapterCredential, AlertSettings, Job, Keyword,
                        KeywordGroup, ProposalQueueItem, SearchProfile,
                        Template, User)
from app.schemas import IngestJobsIn, JobIngest


@pytest.fixture(autouse=True)
def force_offline(monkeypatch):
    """Deterministic offline paths: no LLM, no Redis cache/pacing buckets."""
    monkeypatch.setattr("app.proposal_gen.llm.llm_available", lambda: False)
    monkeypatch.setattr("app.routers.jobs.cache._r", None)
    monkeypatch.setattr("app.ingest.cache._r", None)
    monkeypatch.setattr("app.discovery.cache._r", None)


@pytest.fixture(autouse=True)
def no_broker(monkeypatch):
    """Capture generation enqueues instead of talking to a Celery broker."""
    enqueued = []
    monkeypatch.setattr("app.tasks.generate_proposal_task.delay",
                        lambda job_id: enqueued.append(job_id))
    return enqueued


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def user(db):
    u = User(email="phase2@example.com",
             password_hash=hash_password("password123"), display_name="P2")
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


def _register(client, email="phase2-api@example.com"):
    r = client.post("/api/auth/register",
                    json={"email": email, "password": "password123", "display_name": "P2"})
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _user_id(Session, email="phase2-api@example.com"):
    db = Session()
    try:
        return db.query(User).filter(User.email == email).first().id
    finally:
        db.close()


def _posting(external_id, platform="freelancer", title="React dashboard build"):
    return JobPosting(source_platform=platform, external_id=external_id,
                      title=title, description="react typescript dashboard work",
                      url="https://example.com/job", skills=["React"])


# ---------------- 2.1 scheduled discovery ----------------

class _FakeSearchAdapter:
    queries = []
    platform = "freelancer"

    def __init__(self, db, user_id, **kw):
        pass

    async def search_jobs(self, query, limit=25, **kw):
        type(self).queries.append(query)
        return [_posting(f"{query}-1", platform=type(self).platform)]

    async def close(self):
        pass


class _FakeUpworkSearchAdapter(_FakeSearchAdapter):
    platform = "upwork"


class _FakeLinkedInSearchAdapter(_FakeSearchAdapter):
    platform = "linkedin"


def _patch_search_adapters(monkeypatch, freelancer=None):
    monkeypatch.setattr("app.discovery.FreelancerAdapter",
                        freelancer or _FakeSearchAdapter)
    monkeypatch.setattr("app.discovery.UpworkAgencyAdapter", _FakeUpworkSearchAdapter)
    monkeypatch.setattr("app.discovery.LinkedInJobsAdapter", _FakeLinkedInSearchAdapter)


@pytest.mark.asyncio
async def test_discovery_derives_terms_and_ingests(db, user, no_broker, monkeypatch):
    from app import discovery

    _FakeSearchAdapter.queries = []
    _patch_search_adapters(monkeypatch)

    profile = SearchProfile(user_id=user.id, name="react work",
                            boolean_query="(React OR Vue) AND (NOT WordPress)")
    db.add(profile)
    db.commit()

    result = await discovery.run_profile_discovery(db, user, profile)
    assert result["queued"] is True
    assert result["platforms"] == ["freelancer", "upwork", "linkedin"]
    # positive terms extracted from the boolean query (NOT terms excluded)
    assert set(_FakeSearchAdapter.queries) == {"React", "Vue"}
    # results went through the ingest pipeline
    jobs = db.query(Job).filter(Job.user_id == user.id).all()
    assert len(jobs) == 6  # 2 terms × 3 platforms
    assert result["ingested"] == 6


def test_discovery_terms_fall_back_to_keyword_group(db, user):
    from app.discovery import search_terms_for_profile

    group = KeywordGroup(user_id=user.id, name="stack")
    group.keywords = [Keyword(term="react", kind="primary", weight=1.0),
                      Keyword(term="typescript", kind="primary", weight=0.8),
                      Keyword(term="wordpress", kind="negative", weight=0.0)]
    db.add(group)
    db.commit()
    profile = SearchProfile(user_id=user.id, name="kg profile",
                            keyword_group_id=group.id)
    assert search_terms_for_profile(db, profile) == ["react", "typescript"]


@pytest.mark.asyncio
async def test_discovery_pacing_lock_skips_platform(db, user, monkeypatch):
    from app import discovery

    class _LockedRedis:
        def set(self, key, value, nx=False, ex=None):
            return None  # lock held elsewhere

    monkeypatch.setattr("app.discovery.cache._r", _LockedRedis())
    _FakeSearchAdapter.queries = []
    _patch_search_adapters(monkeypatch)

    profile = SearchProfile(user_id=user.id, name="react", boolean_query="React")
    db.add(profile)
    db.commit()
    result = await discovery.run_profile_discovery(db, user, profile)
    assert result["platforms"] == []  # every platform paced out
    assert _FakeSearchAdapter.queries == []


@pytest.mark.asyncio
async def test_discovery_auth_error_skips_platform(db, user, no_broker, monkeypatch):
    from app import discovery
    from app.adapters.base import AdapterAuthError

    class _AuthFailAdapter(_FakeSearchAdapter):
        async def search_jobs(self, query, limit=25, **kw):
            raise AdapterAuthError("no credentials")

    _FakeSearchAdapter.queries = []
    _patch_search_adapters(monkeypatch, freelancer=_AuthFailAdapter)

    profile = SearchProfile(user_id=user.id, name="react", boolean_query="React")
    db.add(profile)
    db.commit()
    result = await discovery.run_profile_discovery(db, user, profile)
    assert result["platforms"] == ["upwork", "linkedin"]  # freelancer skipped, no crash


def test_run_now_endpoint(client, monkeypatch):
    c, Session = client
    token = _register(c)
    r = c.post("/api/search-profiles", headers=_auth(token),
               json={"name": "react", "boolean_query": "React"})
    assert r.status_code == 201
    profile_id = r.json()["id"]

    async def _fake_discovery(db, user, profile, respect_pacing=True):
        assert respect_pacing is False  # manual runs bypass pacing
        return {"queued": True, "platforms": ["freelancer"], "ingested": 3}

    monkeypatch.setattr("app.discovery.run_profile_discovery", _fake_discovery)
    r = c.post(f"/api/search-profiles/{profile_id}/run-now", headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json() == {"queued": True, "platforms": ["freelancer"]}
    # tenant scoping
    other = _register(c, "phase2-other@example.com")
    assert c.post(f"/api/search-profiles/{profile_id}/run-now",
                  headers=_auth(other)).status_code == 404


# ---------------- 2.2 generation off the request path ----------------

@pytest.mark.asyncio
async def test_ingest_enqueues_generation_without_llm_call(db, user, no_broker, monkeypatch):
    from app.ingest import run_ingest

    generate_calls = []

    async def _no_generate(*a, **kw):
        generate_calls.append(1)
        raise AssertionError("LLM generation must not run on the ingest path")

    monkeypatch.setattr("app.proposal_gen.generate", _no_generate)

    body = IngestJobsIn(jobs=[JobIngest(external_id="off-1", platform="upwork",
                                        title="React app", description="react work")])
    result = await run_ingest(body, db, user)
    assert result.ingested == 1
    assert no_broker and generate_calls == []
    # no queue item yet — the task creates it
    assert db.query(ProposalQueueItem).count() == 0


@pytest.mark.asyncio
async def test_negative_keyword_archived_even_without_filters(db, user, no_broker):
    from app.ingest import run_ingest

    group = KeywordGroup(user_id=user.id, name="neg")
    group.keywords = [Keyword(term="wordpress", kind="negative", weight=0.0)]
    db.add(group)
    db.commit()

    body = IngestJobsIn(jobs=[JobIngest(external_id="neg-1", platform="upwork",
                                        title="WordPress site needed",
                                        description="wordpress theme work")])
    result = await run_ingest(body, db, user)
    assert result.auto_archived == 1 and result.ingested == 0
    job = db.query(Job).filter(Job.external_id == "neg-1").one()
    assert job.status == "archived"
    assert no_broker == []  # excluded jobs never reach generation


@pytest.mark.asyncio
async def test_normal_job_without_filters_still_ingests(db, user, no_broker):
    from app.ingest import run_ingest

    body = IngestJobsIn(jobs=[JobIngest(external_id="ok-1", platform="upwork",
                                        title="React app",
                                        description="react typescript work")])
    result = await run_ingest(body, db, user)
    assert result.ingested == 1 and result.auto_archived == 0
    job = db.query(Job).filter(Job.external_id == "ok-1").one()
    assert job.status != "archived"


def test_generate_proposal_core_creates_queue_item(db, user, monkeypatch):
    Session = sessionmaker(bind=db.get_bind())
    monkeypatch.setattr("app.tasks.SessionLocal", Session)
    job = Job(user_id=user.id, external_id="core-1", platform="upwork",
              title="React app", description="react typescript work",
              skills=["React"], status="new", job_type="fixed")
    db.add(job)
    db.commit()

    from app.tasks import generate_proposal_core
    result = generate_proposal_core(job.id)
    assert result["queued"] is True
    item = db.query(ProposalQueueItem).filter(ProposalQueueItem.job_id == job.id).one()
    assert item.status == "pending_review" and item.proposal_text


# ---------------- 2.3 prompt_hints ----------------

@pytest.mark.asyncio
async def test_prompt_hints_reach_generation_prompt(db, user, monkeypatch):
    captured = {}

    monkeypatch.setattr("app.proposal_gen.llm.llm_available", lambda: True)

    async def _fail_json(*a, **kw):
        raise RuntimeError("force heuristic analysis")

    async def _capture_complete(system, user_prompt, **kw):
        captured["system"] = system
        captured["user"] = user_prompt
        return {"text": "A solid draft.", "model": "mock", "latency_ms": 1}

    monkeypatch.setattr("app.proposal_gen.llm.complete_json", _fail_json)
    monkeypatch.setattr("app.proposal_gen.llm.complete", _capture_complete)

    job = Job(user_id=user.id, external_id="hint-1", platform="upwork",
              title="React dashboard", description="react dashboard work " * 10,
              skills=["React"], job_type="fixed", budget_min=1000, budget_max=2000)
    db.add(job)
    db.commit()

    await proposal_gen.generate(
        db, job, prompt_hints=["Reference more job-specific details."])
    prompt = captured["user"]
    assert "REVIEWER FEEDBACK TO INCORPORATE" in prompt
    assert "- Reference more job-specific details." in prompt
    # operator guidance sits OUTSIDE the untrusted job_posting block
    assert prompt.index("REVIEWER FEEDBACK") > prompt.index("</job_posting>")


# ---------------- 2.4 outcome + reply sync ----------------

class _FakeFreelancerSyncAdapter:
    def __init__(self, db, user_id, **kw):
        pass

    async def get_bid_status(self, bid_id):
        return {"id": bid_id, "status": "awarded"}

    async def get_threads(self, limit=50, offset=0):
        return [{
            "project": {"id": 555},
            "last_message": {"from_user": 999,
                             "time": datetime.now(timezone.utc).timestamp(),
                             "message": "Can you start Monday?"},
        }]

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_outcome_sync_transitions_and_broadcasts(db, user, monkeypatch):
    events = []

    async def _capture(user_id, message):
        events.append(message)

    monkeypatch.setattr("app.outcome_sync.FreelancerAdapter", _FakeFreelancerSyncAdapter)
    monkeypatch.setattr("app.outcome_sync.alerts.broadcast", _capture)

    db.add(AdapterCredential(user_id=user.id, platform="freelancer",
                             principal="default", blob="enc"))
    job = Job(user_id=user.id, external_id="555", platform="freelancer",
              title="Dashboard job")
    db.add(job)
    db.commit()
    item = ProposalQueueItem(
        user_id=user.id, job_id=job.id, platform="freelancer",
        proposal_text="text", status="submitted", outcome="pending",
        reviewed_by="op", reviewed_at=datetime.now(timezone.utc) - timedelta(hours=1),
        submission_result={"response": {"id": 77}, "bidder_id": 4242})
    db.add(item)
    db.commit()

    from app.outcome_sync import sync_user_outcomes
    result = await sync_user_outcomes(db, user)
    assert result["outcomes"] == 1 and result["replies"] == 1
    db.refresh(item)
    assert item.outcome == "hired"
    assert item.client_replied_at is not None
    reply_events = [e for e in events if e["type"] == "client_replied"]
    assert len(reply_events) == 1
    assert reply_events[0]["proposal_id"] == item.id
    assert reply_events[0]["job_id"] == job.id
    assert "Monday" in reply_events[0]["snippet"]

    # idempotent: a second run does not re-broadcast or re-count
    events.clear()
    result = await sync_user_outcomes(db, user)
    assert result["replies"] == 0 and events == []


@pytest.mark.asyncio
async def test_outcome_sync_skips_users_without_credentials(db, user):
    job = Job(user_id=user.id, external_id="556", platform="freelancer", title="J")
    db.add(job)
    db.commit()
    db.add(ProposalQueueItem(user_id=user.id, job_id=job.id, platform="freelancer",
                             status="submitted"))
    db.commit()
    from app.outcome_sync import sync_user_outcomes
    assert (await sync_user_outcomes(db, user))["checked"] == 0


# ---------------- 2.5 template provenance ----------------

def test_approve_reuses_suggested_template(client):
    c, Session = client
    token = _register(c)
    uid = _user_id(Session)
    db = Session()
    try:
        tpl = Template(user_id=uid, title="React tpl", platform="upwork",
                       text="t", uses=4)
        db.add(tpl)
        job = Job(user_id=uid, external_id="prov-1", platform="upwork", title="J")
        db.add(job)
        db.commit()
        item = ProposalQueueItem(user_id=uid, job_id=job.id, platform="upwork",
                                 proposal_text="draft", status="pending_review",
                                 template_id=tpl.id)
        db.add(item)
        db.commit()
        item_id, tpl_id = item.id, tpl.id
    finally:
        db.close()

    r = c.post(f"/api/proposals/{item_id}/approve", headers=_auth(token),
               json={"reviewer": "op"})
    assert r.status_code == 200, r.text
    db = Session()
    try:
        assert db.query(Template).filter(Template.user_id == uid).count() == 1  # no mint
        # uses counted at selection only (N7) — approval no longer increments
        assert db.get(Template, tpl_id).uses == 4
        assert db.get(ProposalQueueItem, item_id).template_id == tpl_id
    finally:
        db.close()


def test_approve_mints_only_when_flag_on(client):
    c, Session = client
    token = _register(c)
    uid = _user_id(Session)

    def _make_item(tag):
        db = Session()
        try:
            job = Job(user_id=uid, external_id=f"prov-{tag}", platform="upwork",
                      title="J")
            db.add(job)
            db.commit()
            item = ProposalQueueItem(user_id=uid, job_id=job.id, platform="upwork",
                                     proposal_text="draft", status="pending_review")
            db.add(item)
            db.commit()
            return item.id
        finally:
            db.close()

    minted_id = _make_item("mint")
    opted_out_id = _make_item("optout")
    # minting is controlled per approval (N2): default True, opt out explicitly
    assert c.post(f"/api/proposals/{minted_id}/approve", headers=_auth(token),
                  json={"reviewer": "op"}).status_code == 200
    assert c.post(f"/api/proposals/{opted_out_id}/approve", headers=_auth(token),
                  json={"reviewer": "op", "save_as_template": False}).status_code == 200
    db = Session()
    try:
        templates = db.query(Template).filter(Template.user_id == uid).all()
        assert len(templates) == 1  # only the flagged item minted one
        assert db.get(ProposalQueueItem, minted_id).template_id == templates[0].id
        opted = db.get(ProposalQueueItem, opted_out_id)
        assert opted.template_id is None
        assert opted.save_as_template is False  # persisted on the item
    finally:
        db.close()


def test_top_templates_counts_uses_at_selection(db, user):
    from app.templates import top_templates

    db.add(Template(user_id=user.id, title="A", platform="upwork", text="t",
                    tags=["react"], uses=0, win_rate=80.0))
    db.commit()
    top = top_templates(db, user.id, "upwork", ["react"])
    assert top[0].uses == 1
    top_templates(db, user.id, "upwork", ["react"])
    db.expire_all()
    assert db.query(Template).one().uses == 2


# ---------------- 2.7 safe automations ----------------

def test_bulk_archive_endpoint(client):
    c, Session = client
    token = _register(c)
    uid = _user_id(Session)
    _register(c, "phase2-third@example.com")
    db = Session()
    try:
        ids = []
        for i in range(2):
            job = Job(user_id=uid, external_id=f"ba-{i}", platform="upwork", title="J")
            db.add(job)
            db.commit()
            ids.append(job.id)
        foreign = Job(user_id=_user_id(Session, "phase2-third@example.com"),
                      external_id="ba-f", platform="upwork", title="J")
        db.add(foreign)
        db.commit()
        foreign_id = foreign.id
    finally:
        db.close()

    r = c.post("/api/jobs/bulk-archive", headers=_auth(token),
               json={"ids": [*ids, foreign_id, 999999]})
    assert r.status_code == 200, r.text
    assert r.json() == {"archived": ids, "skipped": [foreign_id, 999999]}
    db = Session()
    try:
        assert db.get(Job, ids[0]).status == "archived"
        assert db.get(Job, foreign_id).status == "new"  # other tenant untouched
    finally:
        db.close()


def test_auto_archive_tick(db, user, monkeypatch):
    Session = sessionmaker(bind=db.get_bind())
    monkeypatch.setattr("app.tasks.SessionLocal", Session)
    now = datetime.now(timezone.utc)
    stale = Job(user_id=user.id, external_id="aa-1", platform="upwork", title="old",
                status="notified", fetched_at=now - timedelta(days=15))
    expired = Job(user_id=user.id, external_id="aa-2", platform="upwork", title="exp",
                  status="new", apply_deadline=now - timedelta(hours=1))
    fresh = Job(user_id=user.id, external_id="aa-3", platform="upwork", title="new",
                status="new")
    db.add_all([stale, expired, fresh])
    db.commit()

    from app.tasks import auto_archive_tick_core
    result = auto_archive_tick_core()
    assert result["archived"] == 2
    db.expire_all()
    assert stale.status == "archived" and expired.status == "archived"
    assert fresh.status == "new"


def test_digest_tick_fans_out_per_due_user(db, user, monkeypatch):
    """Dispatcher: one digest_user_task per due user; non-due modes skipped."""
    Session = sessionmaker(bind=db.get_bind())
    monkeypatch.setattr("app.tasks.SessionLocal", Session)
    enqueued = []
    monkeypatch.setattr("app.tasks.digest_user_task.delay",
                        lambda user_id: enqueued.append(user_id))

    other = User(email="digest2@example.com",
                 password_hash=hash_password("pw123456"))
    db.add(other)
    db.add(AlertSettings(user_id=user.id, digest_mode="hourly",
                         min_score_alert=50.0))
    db.add(AlertSettings(user_id=other.id, digest_mode="daily",
                         min_score_alert=50.0))
    db.commit()

    from app.tasks import digest_tick_core

    # off the 07:00 UTC hour: only the hourly user is due
    monkeypatch.setattr("app.digest.datetime", _FixedDatetime(12))
    assert digest_tick_core() == {"enqueued": [user.id]}
    # at 07:00 UTC both modes are due
    monkeypatch.setattr("app.digest.datetime", _FixedDatetime(7))
    assert digest_tick_core() == {"enqueued": [user.id, other.id]}
    # 'off' never fires
    db.query(AlertSettings).filter(AlertSettings.user_id == user.id) \
        .update({"digest_mode": "off"})
    db.commit()
    assert digest_tick_core() == {"enqueued": [other.id]}

    # a failed enqueue (broker down) doesn't block the remaining users
    def _flaky(user_id):
        if user_id == other.id:
            raise RuntimeError("broker down")
        enqueued.append(user_id)

    db.query(AlertSettings).filter(AlertSettings.user_id == user.id) \
        .update({"digest_mode": "hourly"})
    db.commit()
    enqueued.clear()
    monkeypatch.setattr("app.tasks.digest_user_task.delay", _flaky)
    assert digest_tick_core() == {"enqueued": [user.id]}


class _FixedDatetime:
    """datetime stand-in: now() pinned to a given UTC hour."""

    def __init__(self, hour):
        self._hour = hour

    def now(self, tz=None):
        return datetime.now(timezone.utc).replace(hour=self._hour)


def test_digest_user_core_sends_and_isolates_failures(db, user, monkeypatch):
    Session = sessionmaker(bind=db.get_bind())
    monkeypatch.setattr("app.tasks.SessionLocal", Session)
    db.add(AlertSettings(user_id=user.id, digest_mode="hourly", min_score_alert=50.0))
    db.add(Job(user_id=user.id, external_id="dg-1", platform="upwork", title="hot",
               status="new", quality_score=80.0))
    db.commit()

    sent = []
    monkeypatch.setattr("app.digest.send_digest_email",
                        lambda jobs, mode: sent.append((mode, len(jobs))) or True)
    from app.tasks import digest_user_core
    assert digest_user_core(user.id) == {"sent": 1}
    assert sent == [("hourly", 1)]

    # nothing to report → no send, no error
    db.query(Job).filter(Job.user_id == user.id).update({"status": "archived"})
    db.commit()
    assert digest_user_core(user.id) == {"sent": 0}

    # SMTP failure is contained to this user's task (no raise)
    def _boom(jobs, mode):
        raise OSError("smtp down")

    db.query(Job).filter(Job.user_id == user.id).update({"status": "new"})
    db.commit()
    monkeypatch.setattr("app.digest.send_digest_email", _boom)
    result = digest_user_core(user.id)
    assert result["sent"] == 0 and "smtp down" in result["error"]


def test_generation_retry_tick(db, user, monkeypatch):
    Session = sessionmaker(bind=db.get_bind())
    monkeypatch.setattr("app.tasks.SessionLocal", Session)
    enqueued = []
    monkeypatch.setattr("app.tasks.generate_proposal_task.delay",
                        lambda job_id: enqueued.append(job_id))

    job = Job(user_id=user.id, external_id="rt-1", platform="upwork", title="J")
    db.add(job)
    db.commit()
    fresh_fail = ProposalQueueItem(user_id=user.id, job_id=job.id, platform="upwork",
                                   status="generation_failed", submission_result={})
    exhausted = ProposalQueueItem(user_id=user.id, job_id=job.id, platform="upwork",
                                  status="generation_failed",
                                  submission_result={"generation_retries": 2})
    db.add_all([fresh_fail, exhausted])
    db.commit()

    from app.tasks import generation_retry_tick_core
    result = generation_retry_tick_core()
    assert result["retried"] == [fresh_fail.id]
    assert enqueued == [job.id]
    db.refresh(fresh_fail)
    assert fresh_fail.submission_result["generation_retries"] == 1


# ---------------- 2.8 dedupe hardening ----------------

def test_find_duplicate_bounded_to_72h(db, user):
    from app.ingest import _find_duplicate

    now = datetime.now(timezone.utc)
    old = Job(user_id=user.id, external_id="dup-old", platform="upwork",
              title="React dashboard build", description="react dashboard " * 20,
              fetched_at=now - timedelta(days=4))
    db.add(old)
    db.commit()

    probe = Job(user_id=user.id, external_id="dup-probe", platform="upwork",
                title="React dashboard build", description="react dashboard " * 20)
    db.add(probe)
    db.flush()
    # the only twin is 4 days old — outside the 72h candidate window
    assert _find_duplicate(db, user.id, probe) is None

    recent = Job(user_id=user.id, external_id="dup-recent", platform="upwork",
                 title="React dashboard build", description="react dashboard " * 20,
                 fetched_at=now - timedelta(hours=2))
    db.add(recent)
    db.commit()
    assert _find_duplicate(db, user.id, probe).id == recent.id


# ---------------- 2.6 analytics funnel ----------------

def test_analytics_funnel(client):
    c, Session = client
    token = _register(c)
    uid = _user_id(Session)
    now = datetime.now(timezone.utc)
    db = Session()
    try:
        job = Job(user_id=uid, external_id="an-1", platform="upwork", title="J")
        db.add(job)
        db.commit()

        def _item(status, outcome="pending", bid=None, replied=False, reason=None):
            it = ProposalQueueItem(
                user_id=uid, job_id=job.id, platform="upwork", proposal_text="t",
                status=status, outcome=outcome, bid_amount=bid,
                rejection_reason=reason,
                client_replied_at=now if replied else None)
            db.add(it)
            return it

        _item("pending_review")
        _item("approved")
        _item("submitted", outcome="hired", bid=750.0, replied=True)
        _item("submitted", outcome="rejected", bid=50.0)
        _item("rejected", reason="too_generic")
        db.add(Template(user_id=uid, title="T", platform="upwork", text="x",
                        uses=3, wins=1, losses=1))
        db.add(Template(user_id=uid, title="U", platform="upwork", text="y"))
        db.commit()
    finally:
        db.close()

    r = c.get("/api/analytics/funnel", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["funnel"] == {"queued": 4, "approved": 3, "submitted": 2,
                              "replied": 1, "hired": 1, "rejected": 1, "ghosted": 0}
    assert body["by_platform"] == [{
        "platform": "upwork", "queued": 4, "approved": 3, "submitted": 2,
        "replied": 1, "hired": 1, "win_rate": 50.0}]
    tpl = {t["title"]: t for t in body["by_template"]}
    assert tpl["T"]["win_rate"] == 50.0 and tpl["T"]["uses"] == 3
    assert tpl["U"]["win_rate"] is None  # no outcomes yet
    bands = {b["band"]: b for b in body["by_bid_band"]}
    assert bands["500-1000"]["submitted"] == 1 and bands["500-1000"]["win_rate"] == 100.0
    assert bands["<100"]["hired"] == 0 and bands["<100"]["win_rate"] == 0.0
    assert bands["1000+"]["win_rate"] is None
    assert body["rejection_reasons"] == [{"reason": "too_generic", "count": 1}]
    # tenant scoping: another user sees empty analytics
    other = _register(c, "phase2-analytics@example.com")
    empty = c.get("/api/analytics/funnel", headers=_auth(other)).json()
    assert empty["funnel"]["queued"] == 0 and empty["by_template"] == []


def test_analytics_funnel_unverified_is_active_not_submitted(client):
    """submitted_unverified is attention-needing/in-flight (queued + approved
    counts) but must NOT inflate the submitted funnel counts."""
    c, Session = client
    token = _register(c, "phase2-unverified@example.com")
    uid = _user_id(Session, "phase2-unverified@example.com")
    db = Session()
    try:
        job = Job(user_id=uid, external_id="an-uv", platform="upwork", title="J")
        db.add(job)
        db.commit()
        db.add(ProposalQueueItem(user_id=uid, job_id=job.id, platform="upwork",
                                 proposal_text="t", status="submitted_unverified"))
        db.commit()
    finally:
        db.close()

    r = c.get("/api/analytics/funnel", headers=_auth(token))
    assert r.status_code == 200, r.text
    funnel = r.json()["funnel"]
    assert funnel["queued"] == 1 and funnel["approved"] == 1
    assert funnel["submitted"] == 0


# ---------------- AD-6 WS over Redis pub/sub ----------------

class _FakeAsyncRedis:
    def __init__(self, fail=False):
        self.published = []
        self.fail = fail

    async def ping(self):
        return True

    async def publish(self, channel, payload):
        if self.fail:
            raise ConnectionError("redis down")
        self.published.append((channel, payload))


@pytest.mark.asyncio
async def test_ws_broadcast_publishes_to_redis():
    from app.ws_manager import AlertManager

    mgr = AlertManager()
    mgr._redis = _FakeAsyncRedis()
    await mgr.broadcast(7, {"type": "client_replied", "proposal_id": 1,
                            "job_id": 2, "snippet": "hi"})
    assert len(mgr._redis.published) == 1
    channel, payload = mgr._redis.published[0]
    assert channel == "gighound:ws:7"
    data = json.loads(payload)
    assert data["user_id"] == 7
    assert data["message"]["type"] == "client_replied"


@pytest.mark.asyncio
async def test_ws_broadcast_falls_back_to_local_when_redis_down():
    from app.ws_manager import AlertManager

    mgr = AlertManager()
    mgr._redis = _FakeAsyncRedis(fail=True)
    local = []

    async def _capture(user_id, message):
        local.append((user_id, message))

    mgr._broadcast_local = _capture
    await mgr.broadcast(3, {"type": "job_ingested", "job": {}})
    assert mgr._redis is None  # poisoned client dropped; retried next time
    assert local == [(3, {"type": "job_ingested", "job": {}})]


@pytest.mark.asyncio
async def test_ws_subscriber_forwards_pubsub_to_local(monkeypatch):
    from app.ws_manager import AlertManager

    frames = [{
        "type": "pmessage",
        "data": json.dumps({"user_id": 9, "message": {"type": "proposal_queued",
                                                      "proposal_id": 5, "job": {}}}),
    }]

    class _FakePubSub:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def psubscribe(self, pattern):
            assert pattern == "gighound:ws:*"

        async def listen(self):
            for frame in frames:
                yield frame
            raise asyncio.CancelledError()  # stop after the scripted frames

    class _FakeSubClient:
        def pubsub(self):
            return _FakePubSub()

        async def aclose(self):
            pass

    monkeypatch.setattr("app.ws_manager.aioredis.from_url",
                        lambda url, **kw: _FakeSubClient())

    mgr = AlertManager()
    received = []

    async def _capture(user_id, message):
        received.append((user_id, message))

    mgr._broadcast_local = _capture
    with pytest.raises(asyncio.CancelledError):
        await mgr._subscribe_loop()
    assert received == [(9, {"type": "proposal_queued", "proposal_id": 5, "job": {}})]
