"""Phase 3 tests: client intelligence (identity, history, adapter mappings),
follow-up drafting, interview prep, and bid-market intelligence (bid_advice
bands + won-bid rate learning)."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import hash_password
from app.database import Base, get_db
from app.main import app
from app.models import (AdapterState, Job, ProposalQueueItem, RateCardEntry,
                        User)


@pytest.fixture(autouse=True)
def force_offline(monkeypatch):
    """Deterministic offline paths: no LLM, no Redis cache/pacing buckets."""
    monkeypatch.setattr("app.proposal_gen.llm.llm_available", lambda: False)
    monkeypatch.setattr("app.routers.jobs.cache._r", None)
    monkeypatch.setattr("app.ingest.cache._r", None)


@pytest.fixture(autouse=True)
def no_broker(monkeypatch):
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
    u = User(email="phase3@example.com",
             password_hash=hash_password("password123"), display_name="P3")
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


def _register(client, email="phase3-api@example.com"):
    r = client.post("/api/auth/register",
                    json={"email": email, "password": "password123", "display_name": "P3"})
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _user_id(Session, email="phase3-api@example.com"):
    db = Session()
    try:
        return db.query(User).filter(User.email == email).first().id
    finally:
        db.close()


ANALYSIS = {
    "required_skills": ["react", "typescript"],
    "deliverables": ["dashboard MVP"],
    "client_pain_points": ["current dashboard is slow"],
    "tone": "technical",
    "missing_info": ["budget"],
    "red_flags": [],
    "strengths": ["react"],
    "gaps": [],
}


def _seed_submitted_item(db, uid, **overrides):
    job = Job(user_id=uid, external_id=overrides.pop("ext", "p3-1"),
              platform=overrides.get("platform", "freelancer"),
              title="React dashboard build",
              description="react typescript dashboard work " * 10,
              skills=["React"], client_info=overrides.pop("client_info", {}))
    db.add(job)
    db.commit()
    fields = dict(
        user_id=uid, job_id=job.id, platform="freelancer",
        proposal_text="My original proposal text.", status="submitted",
        outcome="pending", analysis=ANALYSIS,
        portfolio_match={"1": {"title": "SaaS analytics dashboard", "overlap_pct": 80,
                               "matched_skills": ["react"]}},
    )
    fields.update(overrides)
    item = ProposalQueueItem(**fields)
    db.add(item)
    db.commit()
    return job, item


# ---------------- 3.2 follow-up drafting ----------------

def test_follow_up_endpoint_creates_pending_item(client):
    c, Session = client
    token = _register(c)
    uid = _user_id(Session)
    db = Session()
    try:
        job, item = _seed_submitted_item(db, uid)
        item_id, job_id = item.id, job.id
    finally:
        db.close()

    r = c.post(f"/api/proposals/{item_id}/follow-up", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] != item_id
    assert body["status"] == "pending_review"
    assert body["request_type"] == "follow_up"
    assert body["job_id"] == job_id and body["platform"] == "freelancer"
    assert body["submission_result"]["parent_proposal_id"] == item_id
    assert body["proposal_text"]  # offline composer produced a draft


@pytest.mark.asyncio
async def test_follow_up_offline_composer_references_original(db, user):
    from app import proposal_gen

    job, item = _seed_submitted_item(db, user.id)
    gen = await proposal_gen.generate_follow_up(db, item, job)
    text = gen["draft_text"]
    assert "React dashboard build" in text[:120]  # references the original bid
    assert "budget" in text  # one new question derived from missing_info


def test_follow_up_gating(client):
    c, Session = client
    token = _register(c)
    uid = _user_id(Session)
    db = Session()
    try:
        _, draft = _seed_submitted_item(db, uid, ext="g-1", status="pending_review")
        _, hired = _seed_submitted_item(db, uid, ext="g-2", outcome="hired")
        _, submitted = _seed_submitted_item(db, uid, ext="g-3")
        ids = (draft.id, hired.id, submitted.id)
    finally:
        db.close()

    # not submitted-ish → 409
    assert c.post(f"/api/proposals/{ids[0]}/follow-up",
                  headers=_auth(token)).status_code == 409
    # terminal outcome → 409
    assert c.post(f"/api/proposals/{ids[1]}/follow-up",
                  headers=_auth(token)).status_code == 409
    # happy path, then a second pending follow-up → 409
    assert c.post(f"/api/proposals/{ids[2]}/follow-up",
                  headers=_auth(token)).status_code == 200
    assert c.post(f"/api/proposals/{ids[2]}/follow-up",
                  headers=_auth(token)).status_code == 409
    # queued_for_browser (upwork handoff) is also follow-up eligible
    db = Session()
    try:
        _, queued = _seed_submitted_item(db, uid, ext="g-4",
                                         status="queued_for_browser", platform="upwork")
        queued_id = queued.id
    finally:
        db.close()
    assert c.post(f"/api/proposals/{queued_id}/follow-up",
                  headers=_auth(token)).status_code == 200


# ---------------- 3.3 interview prep ----------------

def test_interview_prep_offline_shape_and_caching(client, monkeypatch):
    c, Session = client
    token = _register(c)
    uid = _user_id(Session)
    db = Session()
    try:
        _, item = _seed_submitted_item(db, uid)
        item_id = item.id
    finally:
        db.close()

    calls = []

    async def _spy(db_, item_, job_):
        calls.append(1)
        from app import proposal_gen
        return proposal_gen._interview_prep_offline(
            job_, item_, item_.analysis or {},
            [pm["title"] for pm in (item_.portfolio_match or {}).values()])

    monkeypatch.setattr("app.proposal_gen.generate_interview_prep", _spy)

    r = c.get(f"/api/proposals/{item_id}/interview-prep", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["questions"]) == 5
    assert all(set(q) == {"question", "suggested_answer"} for q in body["questions"])
    assert body["pain_points"] == ["current dashboard is slow"]
    assert body["red_flags"] == []
    assert body["talking_points"]
    # grounded in the portfolio + derived from missing_info
    assert any("SaaS analytics dashboard" in q["suggested_answer"]
               for q in body["questions"])
    assert any("budget" in q["suggested_answer"] for q in body["questions"])
    assert calls  # generator ran once

    # second GET is served from the item's cache — generator not called again
    r2 = c.get(f"/api/proposals/{item_id}/interview-prep", headers=_auth(token))
    assert r2.status_code == 200 and r2.json() == body
    assert len(calls) == 1
    db = Session()
    try:
        cached = db.get(ProposalQueueItem, item_id).submission_result["interview_prep"]
        assert cached == body
    finally:
        db.close()


# ---------------- 3.1 client intelligence ----------------

def test_upwork_normalize_populates_past_hires():
    from app.adapters.upwork_agency import UpworkAgencyAdapter

    node = {
        "id": "abc123", "ciphertext": "~01abc", "title": "Full-stack dev",
        "description": "Django + React", "skills": [],
        "client": {"id": "cli-9", "totalHires": 20, "totalPostedJobs": 25,
                   "totalSpent": {"rawValue": "30000"},
                   "paymentVerificationStatus": "VERIFIED",
                   "location": {"country": "United States"}},
    }
    j = UpworkAgencyAdapter._normalize(node)
    assert j.client_info.past_hires == 20
    assert j.client_info.payment_verified is True
    assert j.client_info.client_id == "cli-9"
    # missing client id → None, no crash
    node["client"].pop("id")
    assert UpworkAgencyAdapter._normalize(node).client_info.client_id is None


def test_freelancer_normalize_populates_owner_intel():
    from app.adapters.freelancer import FreelancerAdapter

    project = {
        "id": 12345, "title": "Build a React app", "description": "react",
        "type": "FIXED", "budget": {"minimum": 500, "maximum": 1000},
        "currency": {"code": "USD"},
        "owner": {"id": 42, "username": "acme_corp",
                  "status": {"payment_verified": True, "identity_verified": True},
                  "location": {"country": {"name": "United States"}},
                  "reputation": {"entire_history": {"overall": 4.8, "reviews": 30}}},
    }
    j = FreelancerAdapter._normalize_project(project)
    assert j.client_info.identity_verified is True
    assert j.client_info.payment_verified is True
    assert j.client_info.client_id == "42"
    assert j.client_info.name == "acme_corp"


def test_client_history_aggregation_and_null(client):
    c, Session = client
    token = _register(c)
    uid = _user_id(Session)
    db = Session()
    try:
        past_job = Job(user_id=uid, external_id="ch-1", platform="upwork", title="Old job",
                       client_info={"client_id": "cli-77", "country": "United States"})
        other_client_job = Job(user_id=uid, external_id="ch-2", platform="upwork",
                               title="Unrelated", client_info={"client_id": "cli-99"})
        db.add_all([past_job, other_client_job])
        db.commit()
        for outcome in ("hired", "ghosted", "pending"):
            db.add(ProposalQueueItem(user_id=uid, job_id=past_job.id, platform="upwork",
                                     proposal_text="t", status="submitted", outcome=outcome))
        db.add(ProposalQueueItem(user_id=uid, job_id=other_client_job.id, platform="upwork",
                                 proposal_text="t", status="submitted", outcome="rejected"))
        # the job being viewed: same client, no proposals of its own yet
        new_job = Job(user_id=uid, external_id="ch-3", platform="upwork", title="New job",
                      client_info={"client_id": "cli-77"})
        unseen_job = Job(user_id=uid, external_id="ch-4", platform="upwork", title="Mystery",
                         client_info={})
        db.add_all([new_job, unseen_job])
        db.commit()
        new_id, unseen_id = new_job.id, unseen_job.id
    finally:
        db.close()

    r = c.get(f"/api/jobs/{new_id}", headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["client_history"] == {
        "past_proposals": 3, "hired": 1, "rejected": 0, "ghosted": 1}
    # never-seen client → null
    r = c.get(f"/api/jobs/{unseen_id}", headers=_auth(token))
    assert r.json()["client_history"] is None


def test_client_history_composite_identity_fallback(client):
    c, Session = client
    token = _register(c)
    uid = _user_id(Session)
    db = Session()
    try:
        ci = {"country": "Germany", "rating": 4.9, "total_spent": 12000.0}
        old = Job(user_id=uid, external_id="cx-1", platform="freelancer", title="Old",
                  client_info=ci)
        db.add(old)
        db.commit()
        db.add(ProposalQueueItem(user_id=uid, job_id=old.id, platform="freelancer",
                                 proposal_text="t", status="submitted", outcome="hired"))
        # same composite (12k lands in the same spent bucket as 20k)
        new = Job(user_id=uid, external_id="cx-2", platform="freelancer", title="New",
                  client_info={"country": "Germany", "rating": 4.9, "total_spent": 20000.0})
        # different country → different composite
        other = Job(user_id=uid, external_id="cx-3", platform="freelancer", title="Other",
                    client_info={"country": "France", "rating": 4.9, "total_spent": 12000.0})
        db.add_all([new, other])
        db.commit()
        new_id, other_id = new.id, other.id
    finally:
        db.close()

    r = c.get(f"/api/jobs/{new_id}", headers=_auth(token))
    assert r.json()["client_history"] == {
        "past_proposals": 1, "hired": 1, "rejected": 0, "ghosted": 0}
    r = c.get(f"/api/jobs/{other_id}", headers=_auth(token))
    assert r.json()["client_history"] is None


@pytest.mark.asyncio
async def test_client_history_reaches_generation_prompt(db, user, monkeypatch):
    from app import proposal_gen

    captured = {}

    async def _fail_json(*a, **kw):
        raise RuntimeError("force heuristic analysis")

    async def _capture_complete(system, user_prompt, **kw):
        captured["user"] = user_prompt
        return {"text": "A draft.", "model": "mock", "latency_ms": 1}

    monkeypatch.setattr("app.proposal_gen.llm.llm_available", lambda: True)
    monkeypatch.setattr("app.proposal_gen.llm.complete_json", _fail_json)
    monkeypatch.setattr("app.proposal_gen.llm.complete", _capture_complete)

    ci = {"client_id": "cli-5"}
    past = Job(user_id=user.id, external_id="hp-1", platform="upwork", title="Past",
               client_info=ci)
    db.add(past)
    db.commit()
    db.add(ProposalQueueItem(user_id=user.id, job_id=past.id, platform="upwork",
                             proposal_text="t", status="submitted", outcome="ghosted"))
    job = Job(user_id=user.id, external_id="hp-2", platform="upwork", title="React app",
              description="react work " * 20, skills=["React"], client_info=ci,
              job_type="fixed", budget_min=1000, budget_max=2000)
    db.add(job)
    db.commit()

    await proposal_gen.generate(db, job)
    prompt = captured["user"]
    assert "CLIENT HISTORY: you've bid 1x for this client before: 0 hired, 0 rejected, 1 ghosted" in prompt
    # operator context sits OUTSIDE the untrusted job_posting block
    assert prompt.index("CLIENT HISTORY") > prompt.index("</job_posting>")


# ---------------- 3.4 bid-market intelligence ----------------

def _job_with_count(n, score=50.0):
    return Job(user_id=1, external_id=f"ba-{n}-{score}", platform="upwork",
               title="J", proposals_count=n, quality_score=score)


def test_bid_advice_bands():
    from app.client_intel import compute_bid_advice

    assert compute_bid_advice(_job_with_count(None)) is None
    assert compute_bid_advice(_job_with_count(5))["recommendation"] == "bid"
    assert compute_bid_advice(_job_with_count(20))["recommendation"] == "caution"
    # >25 proposals but a strong job is still just a caution
    assert compute_bid_advice(_job_with_count(30, score=80.0))["recommendation"] == "caution"
    skip = compute_bid_advice(_job_with_count(30, score=50.0))
    assert skip["recommendation"] == "skip" and "30" in skip["reason"]


@pytest.mark.asyncio
async def test_bid_advice_stored_at_queue_time(db, user, no_broker):
    from app.orchestrator import maybe_queue_proposal

    job = Job(user_id=user.id, external_id="q-1", platform="upwork",
              title="React dashboard", description="react typescript dashboard " * 10,
              skills=["React"], job_type="fixed", proposals_count=20,
              quality_score=55.0)
    db.add(job)
    db.commit()
    item = await maybe_queue_proposal(db, job)
    assert item is not None
    assert item.bid_advice["recommendation"] == "caution"
    assert "20" in item.bid_advice["reason"]


def test_won_bids_nudge_calculate_bid(db, user):
    from app.proposal_gen import calculate_bid
    from app.rate_learning import record_winning_bid

    db.add(RateCardEntry(user_id=user.id, skill_category="react", hourly_rate=50))
    job = Job(user_id=user.id, external_id="rb-1", platform="upwork",
              title="React dashboard", description="react typescript dashboard " * 20,
              skills=["React"], job_type="fixed")
    db.add(job)
    db.commit()

    base, _, base_rationale = calculate_bid(db, job, {})
    assert "nudged" not in base_rationale  # no samples yet

    for amt in (base * 1.6, base * 1.7, base * 1.5):
        record_winning_bid(db, user.id, "react", amt)
    nudged, _, rationale = calculate_bid(db, job, {})
    assert base < nudged <= base * 1.2  # pulled up, bounded at +20%
    assert "nudged toward 3 past winning bids" in rationale


def test_record_outcome_hired_stores_winning_bid(db, user):
    from app.rate_learning import winning_bid_samples
    from app.templates import record_outcome

    db.add(RateCardEntry(user_id=user.id, skill_category="react", hourly_rate=50))
    job = Job(user_id=user.id, external_id="ro-1", platform="upwork", title="React app",
              skills=["React"])
    db.add(job)
    db.commit()
    item = ProposalQueueItem(user_id=user.id, job_id=job.id, platform="upwork",
                             proposal_text="t", status="submitted", bid_amount=900.0)
    db.add(item)
    db.commit()

    record_outcome(db, item, "hired")
    samples = winning_bid_samples(db, user.id, "react")
    assert len(samples) == 1 and samples[0]["bid_amount"] == 900.0
    assert samples[0]["at"]
    row = db.query(AdapterState).filter_by(user_id=user.id, key="rate_feedback:react").one()
    assert row.value["samples"][0]["bid_amount"] == 900.0

    # non-hired outcomes record nothing
    item2 = ProposalQueueItem(user_id=user.id, job_id=job.id, platform="upwork",
                              proposal_text="t", status="submitted", bid_amount=700.0)
    db.add(item2)
    db.commit()
    record_outcome(db, item2, "rejected")
    assert len(winning_bid_samples(db, user.id, "react")) == 1


# ---------------- P3-3: version history preserves the pre-edit draft ----------------

def test_approve_versions_previous_text(client):
    c, Session = client
    token = _register(c)
    uid = _user_id(Session)
    db = Session()
    try:
        _, item = _seed_submitted_item(db, uid, ext="ver-1", status="pending_review",
                                       proposal_text="The original AI draft.")
        item.versions = []
        db.commit()
        item_id = item.id
    finally:
        db.close()

    # first edit-approve versions the PREVIOUS text (v1 = the AI draft)
    r = c.post(f"/api/proposals/{item_id}/approve", headers=_auth(token),
               json={"reviewer": "P3", "proposal_text": "Edited by the reviewer."})
    assert r.status_code == 200, r.text
    versions = r.json()["versions"]
    assert len(versions) == 1
    assert versions[0]["text"] == "The original AI draft."
    assert versions[0]["by"] == "P3"

    # revert to v1 restores the AI draft and re-enters the review boundary
    r = c.post(f"/api/proposals/{item_id}/revert", headers=_auth(token),
               json={"version_index": 0})
    assert r.status_code == 200, r.text
    assert r.json()["proposal_text"] == "The original AI draft."
    assert r.json()["status"] == "pending_review"

    # a subsequent edit-approve versions the text live at that moment
    r = c.post(f"/api/proposals/{item_id}/approve", headers=_auth(token),
               json={"reviewer": "P3", "proposal_text": "Second round of edits."})
    assert r.status_code == 200, r.text
    versions = r.json()["versions"]
    assert len(versions) == 2
    assert versions[-1]["text"] == "The original AI draft."


# ---------------- P3-4: request_type filter, generation retry, auto-archive ----------------

def test_proposals_list_filters_request_type(client):
    c, Session = client
    token = _register(c)
    uid = _user_id(Session)
    db = Session()
    try:
        _seed_submitted_item(db, uid, ext="rt-1", status="pending_review")
        _, br = _seed_submitted_item(db, uid, ext="rt-2", status="pending_review",
                                     request_type="buyer_request")
        br_id = br.id
    finally:
        db.close()

    r = c.get("/api/proposals?request_type=buyer_request", headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    assert [i["id"] for i in body["items"]] == [br_id]
    # unfiltered list still returns everything
    assert c.get("/api/proposals", headers=_auth(token)).json()["total"] == 2


def test_retry_generation_requeues_failed_item(client, no_broker):
    c, Session = client
    token = _register(c)
    uid = _user_id(Session)
    db = Session()
    try:
        job, failed = _seed_submitted_item(
            db, uid, ext="rg-1", status="generation_failed",
            submission_result={"error": "llm timeout", "generation_retries": 2})
        _, pending = _seed_submitted_item(db, uid, ext="rg-2", status="pending_review")
        job_id, failed_id, pending_id = job.id, failed.id, pending.id
    finally:
        db.close()

    # only generation_failed is retryable
    assert c.post(f"/api/proposals/{pending_id}/retry-generation",
                  headers=_auth(token)).status_code == 409
    assert c.post("/api/proposals/999999/retry-generation",
                  headers=_auth(token)).status_code == 404

    r = c.post(f"/api/proposals/{failed_id}/retry-generation", headers=_auth(token))
    assert r.status_code == 200, r.text
    # retry budget reset; the job's generation task re-enqueued — its core
    # regenerates THIS row, so the status stays until the task lands
    assert r.json()["submission_result"]["generation_retries"] == 0
    assert r.json()["status"] == "generation_failed"
    assert no_broker == [job_id]


def test_auto_archive_spares_jobs_with_live_queue_items(db, user, monkeypatch):
    Session = sessionmaker(bind=db.get_bind())
    monkeypatch.setattr("app.tasks.SessionLocal", Session)
    stale = datetime.now(timezone.utc) - timedelta(days=15)

    def _job(ext):
        return Job(user_id=user.id, external_id=ext, platform="upwork", title="old",
                   status="new", fetched_at=stale)

    live = _job("arc-live")
    unverified = _job("arc-unverified")
    terminal = _job("arc-term")
    plain = _job("arc-none")
    db.add_all([live, unverified, terminal, plain])
    db.commit()
    db.add(ProposalQueueItem(user_id=user.id, job_id=live.id, platform="upwork",
                             proposal_text="t", status="pending_review"))
    db.add(ProposalQueueItem(user_id=user.id, job_id=unverified.id, platform="upwork",
                             proposal_text="t", status="submitted_unverified"))
    db.add(ProposalQueueItem(user_id=user.id, job_id=terminal.id, platform="upwork",
                             proposal_text="t", status="submitted"))
    db.commit()

    from app.tasks import auto_archive_tick_core
    result = auto_archive_tick_core()
    assert result["archived"] == 2
    db.expire_all()
    assert live.status == "new"  # live queue item shields the job
    assert unverified.status == "new"  # unverified still needs human attention
    assert terminal.status == "archived"  # terminal items don't shield
    assert plain.status == "archived"
