"""Phase 1 tests: submission unbreak (settings fallbacks, queued_for_browser),
pitch-template rendering, filter/duplicate gating, platform kill switches,
score-preview endpoint, and word-boundary matching."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import proposal_gen
from app.auth import hash_password
from app.boolquery import matches_boolean_query
from app.database import Base, get_db
from app.main import app
from app.models import (Job, PlatformAccount, PortfolioItem, ProfileTemplate,
                        ProposalQueueItem, RateCardEntry, SearchFilter,
                        SearchProfile, User)
from app.orchestrator import maybe_queue_proposal
from app.proposal_gen import render_pitch_template
from app.schemas import ClientInfo, JobIngest
from app.scoring import compute_quality_score, estimate_complexity


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


@pytest.fixture()
def user(db):
    u = User(email="phase1@example.com",
             password_hash=hash_password("password123"), display_name="P1")
    db.add(u)
    db.commit()
    return u


def _register(client, email="phase1-api@example.com"):
    r = client.post("/api/auth/register",
                    json={"email": email, "password": "password123", "display_name": "P1"})
    assert r.status_code == 201, r.text
    return r.json()["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _user_id(Session, email="phase1-api@example.com"):
    db = Session()
    try:
        return db.query(User).filter(User.email == email).first().id
    finally:
        db.close()


_item_seq = 0


def _make_item(Session, user_id, platform="upwork", status="approved",
               submission_result=None, reviewed_by="operator"):
    global _item_seq
    _item_seq += 1
    db = Session()
    try:
        job = Job(user_id=user_id, external_id=str(20000 + _item_seq),
                  platform=platform, title=f"Job for {platform}")
        db.add(job)
        db.commit()
        item = ProposalQueueItem(
            user_id=user_id, job_id=job.id, platform=platform,
            proposal_text="queued proposal text", bid_amount=500.0, status=status,
            reviewed_by=reviewed_by, submission_result=submission_result or {},
        )
        db.add(item)
        db.commit()
        return item.id
    finally:
        db.close()


def _add_account(Session, user_id, platform, settings=None, enabled=True, mode="api"):
    db = Session()
    try:
        acct = PlatformAccount(user_id=user_id, platform=platform,
                               label=f"{platform} acct", settings=settings or {},
                               enabled=enabled, mode=mode)
        db.add(acct)
        db.commit()
        return acct.id
    finally:
        db.close()


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


# ---------------- 1.2 submit fallbacks + queued_for_browser ----------------

def test_submit_freelancer_bidder_id_from_account_settings(client, monkeypatch):
    c, Session = client
    _FakeFreelancerAdapter.calls = []
    monkeypatch.setattr("app.adapters.freelancer.FreelancerAdapter", _FakeFreelancerAdapter)
    token = _register(c)
    uid = _user_id(Session)
    _add_account(Session, uid, "freelancer", settings={"bidder_id": 4242})
    item_id = _make_item(Session, uid, platform="freelancer")  # no submission_result.bidder_id

    r = c.post(f"/api/proposals/{item_id}/submit", headers=_auth(token))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "submitted"  # freelancer bids are truly submitted
    assert _FakeFreelancerAdapter.calls[0]["bidder_id"] == 4242


def test_submit_freelancer_missing_bidder_id_is_a_clear_400(client, monkeypatch):
    c, Session = client
    _FakeFreelancerAdapter.calls = []
    monkeypatch.setattr("app.adapters.freelancer.FreelancerAdapter", _FakeFreelancerAdapter)
    token = _register(c)
    uid = _user_id(Session)
    item_id = _make_item(Session, uid, platform="freelancer")

    r = c.post(f"/api/proposals/{item_id}/submit", headers=_auth(token))
    assert r.status_code == 400
    assert "Accounts page" in r.json()["detail"]
    assert _FakeFreelancerAdapter.calls == []


def test_submit_upwork_queues_for_browser_with_member_fallback(client, monkeypatch):
    c, Session = client
    _FakeUpworkAdapter.calls = []
    monkeypatch.setattr("app.adapters.upwork_agency.UpworkAgencyAdapter", _FakeUpworkAdapter)
    token = _register(c)
    uid = _user_id(Session)
    _add_account(Session, uid, "upwork", settings={"on_behalf_of": "agency_jane"})
    item_id = _make_item(Session, uid, platform="upwork")

    r = c.post(f"/api/proposals/{item_id}/submit", headers=_auth(token))
    assert r.status_code == 200, r.text
    # truthful state: the browser worker has not confirmed anything yet
    assert r.json()["status"] == "queued_for_browser"
    assert _FakeUpworkAdapter.calls[0]["on_behalf_of"] == "agency_jane"


def test_submit_upwork_missing_member_is_a_clear_400(client, monkeypatch):
    c, Session = client
    _FakeUpworkAdapter.calls = []
    monkeypatch.setattr("app.adapters.upwork_agency.UpworkAgencyAdapter", _FakeUpworkAdapter)
    token = _register(c)
    uid = _user_id(Session)
    item_id = _make_item(Session, uid, platform="upwork")

    r = c.post(f"/api/proposals/{item_id}/submit", headers=_auth(token))
    assert r.status_code == 400
    assert "Accounts page" in r.json()["detail"]
    assert _FakeUpworkAdapter.calls == []


# ---------------- 1.6 score-preview endpoint ----------------

def test_score_preview_scores_without_persisting(client):
    c, Session = client
    token = _register(c)
    uid = _user_id(Session)
    r = c.post("/api/jobs/score-preview", headers=_auth(token), json={"job": {
        "external_id": "preview-1", "platform": "upwork",
        "title": "Senior React developer for analytics dashboard",
        "description": "Deliverables: responsive UI. " + "react typescript graphql " * 12,
        "job_type": "fixed", "budget_min": 4000, "budget_max": 6000,
        "client_info": {"payment_verified": True},
    }})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"quality_score", "score_breakdown", "red_flags"}
    assert body["quality_score"] > 0
    db = Session()
    try:
        assert db.query(Job).filter(Job.user_id == uid).count() == 0
        assert db.query(ProposalQueueItem).filter(ProposalQueueItem.user_id == uid).count() == 0
    finally:
        db.close()


def test_score_preview_negative_keyword_matches_ingest_semantics(client):
    c, Session = client
    token = _register(c)
    uid = _user_id(Session)
    db = Session()
    try:
        from app.models import Keyword, KeywordGroup
        group = KeywordGroup(user_id=uid, name="g")
        group.keywords = [Keyword(term="wordpress", kind="negative", weight=0.0)]
        db.add(group)
        db.commit()
    finally:
        db.close()
    r = c.post("/api/jobs/score-preview", headers=_auth(token), json={"job": {
        "external_id": "preview-2", "platform": "upwork",
        "title": "WordPress site needed", "description": "wordpress theme work",
    }})
    assert r.status_code == 200
    assert r.json()["quality_score"] == 0.0
    assert "negative keyword match" in r.json()["red_flags"]


# ---------------- 1.4 filter gating + is_duplicate gate ----------------

def _queueable_job(user_id, **kw):
    base = dict(external_id="q1", platform="upwork", title="React app",
                description="react typescript work", skills=["React"],
                job_type="fixed", status="new", quality_score=80.0)
    base.update(kw)
    return Job(user_id=user_id, **base)


@pytest.mark.asyncio
async def test_search_filter_gates_queue(db, user):
    flt = SearchFilter(user_id=user.id, name="high bar", platforms=["upwork"],
                       quality_threshold=95.0)
    db.add(flt)
    db.commit()
    db.add(SearchProfile(user_id=user.id, name="react", boolean_query="React",
                         filter_id=flt.id, auto_queue_proposals=True))
    db.commit()
    job = _queueable_job(user.id)
    db.add(job)
    db.commit()

    # score 80 < filter threshold 95 → no draft
    assert await maybe_queue_proposal(db, job) is None
    assert db.query(ProposalQueueItem).count() == 0

    # clears the filter → queued
    job.quality_score = 96.0
    db.commit()
    item = await maybe_queue_proposal(db, job)
    assert item is not None and item.status == "pending_review"


@pytest.mark.asyncio
async def test_filterless_matching_profile_still_queues(db, user):
    db.add(SearchProfile(user_id=user.id, name="react", boolean_query="React",
                         auto_queue_proposals=True))
    job = _queueable_job(user.id)
    db.add(job)
    db.commit()
    assert await maybe_queue_proposal(db, job) is not None


@pytest.mark.asyncio
async def test_duplicate_jobs_never_get_a_draft(db, user):
    job = _queueable_job(user.id, is_duplicate=True, duplicate_of=1)
    db.add(job)
    db.commit()
    assert await maybe_queue_proposal(db, job) is None
    assert db.query(ProposalQueueItem).count() == 0


# ---------------- 1.5 kill switches ----------------

@pytest.mark.asyncio
async def test_disabled_platform_blocks_queueing(db, user):
    db.add(PlatformAccount(user_id=user.id, platform="upwork", label="uw",
                           enabled=False, settings={}))
    job = _queueable_job(user.id)
    db.add(job)
    db.commit()
    assert await maybe_queue_proposal(db, job) is None
    assert db.query(ProposalQueueItem).count() == 0


@pytest.mark.asyncio
async def test_disabled_mode_blocks_queueing(db, user):
    db.add(PlatformAccount(user_id=user.id, platform="upwork", label="uw",
                           mode="disabled", settings={}))
    job = _queueable_job(user.id)
    db.add(job)
    db.commit()
    assert await maybe_queue_proposal(db, job) is None


def test_disabled_platform_blocks_search(client):
    c, Session = client
    token = _register(c)
    uid = _user_id(Session)
    _add_account(Session, uid, "freelancer", enabled=False)
    r = c.post("/api/adapters/freelancer/search", headers=_auth(token), json={})
    assert r.status_code == 409
    assert "disabled" in r.json()["detail"]


def test_disabled_platform_blocks_bid(client, monkeypatch):
    c, Session = client
    _FakeFreelancerAdapter.calls = []
    monkeypatch.setattr("app.routers.adapters.FreelancerAdapter", _FakeFreelancerAdapter)
    token = _register(c)
    uid = _user_id(Session)
    _add_account(Session, uid, "freelancer", enabled=False)
    item_id = _make_item(Session, uid, platform="freelancer",
                         submission_result={"bidder_id": 777})
    r = c.post("/api/adapters/freelancer/bid", headers=_auth(token),
               json={"proposal_queue_item_id": item_id})
    assert r.status_code == 409
    assert _FakeFreelancerAdapter.calls == []


def test_no_account_row_means_platform_allowed(client, monkeypatch):
    c, Session = client
    _FakeFreelancerAdapter.calls = []
    monkeypatch.setattr("app.routers.adapters.FreelancerAdapter", _FakeFreelancerAdapter)
    token = _register(c)
    uid = _user_id(Session)
    item_id = _make_item(Session, uid, platform="freelancer",
                         submission_result={"bidder_id": 777})
    r = c.post("/api/adapters/freelancer/bid", headers=_auth(token),
               json={"proposal_queue_item_id": item_id})
    assert r.status_code == 200, r.text


# ---------------- 1.7 word-boundary matching ----------------

def test_complexity_terms_use_word_boundaries():
    # "ai" ⊂ "said", "api" ⊂ "capital" must not inflate complexity
    assert estimate_complexity("He said the capital is lovely") == 0
    assert estimate_complexity("we need ai and api work") == 2


def test_negative_keyword_uses_word_boundaries():
    class KW:
        def __init__(self, term, kind, weight=1.0):
            self.term, self.kind, self.weight = term, kind, weight

    kws = [KW("php", "negative", 0.0)]
    job = JobIngest(external_id="w1", platform="upwork", title="GraphPHP integration",
                    description="graphPHP sdk work, react frontend " * 8,
                    job_type="fixed", budget_min=3000, budget_max=5000,
                    client_info=ClientInfo(payment_verified=True),
                    posted_at=datetime.now(timezone.utc) - timedelta(minutes=5))
    assert compute_quality_score(job, kws)["quality_score"] > 0  # graphPHP survives
    excluded = compute_quality_score(
        job.model_copy(update={"description": "must know php. " + job.description}), kws)
    assert excluded["quality_score"] == 0.0  # standalone php still excludes


def test_boolean_single_word_terms_use_word_boundaries():
    assert matches_boolean_query("ai", "he said so") is False
    assert matches_boolean_query("ai", "ai engineer") is True
    # quoted phrases keep substring semantics
    assert matches_boolean_query('"ai"', "he said so") is True
    assert matches_boolean_query("(React OR Next.js) AND (NOT WordPress)",
                                 "next.js developer needed") is True


# ---------------- 1.3 pitch template rendering ----------------

def test_render_pitch_template_tokens_and_fallbacks():
    job = Job(user_id=1, external_id="x", platform="upwork", title="Build API",
              client_info={})
    analysis = {"deliverables": ["REST endpoints"], "missing_info": ["budget"],
                "required_skills": ["python"]}
    match = {"portfolio_match": {"1": {"title": "API Gateway"}}}
    out = render_pitch_template(
        "{{client_name}}|{{job_title}}|{{deliverable}}|{{portfolio_piece}}|"
        "{{clarifying_question}}|{{price}}|{{your_name}}|{{rate_line}}|{{unknown}}",
        job, analysis, match, rate_line="$75/hr", bid_amount=500.0, bid_days=14,
        sender_name="Dima")
    assert out == ("there|Build API|REST endpoints|API Gateway|"
                   "what's the budget you're targeting?|$500|Dima|$75/hr|")
    # empty analysis → sane fallbacks, never raw braces
    out2 = render_pitch_template("{{deliverable}}|{{portfolio_piece}}|"
                                 "{{clarifying_question}}|{{price}}",
                                 job, {}, {"portfolio_match": {}},
                                 rate_line="rate on request", bid_amount=None,
                                 bid_days=None, sender_name="Dima")
    assert "{{" not in out2
    assert out2.startswith("the core deliverable|available on request|")
    assert out2.endswith("|a fair price")


@pytest.mark.asyncio
async def test_pitch_template_rendered_end_to_end_offline(db, user):
    db.add(ProfileTemplate(user_id=user.id, platform="upwork", name="Pitch",
                           pitch_template=(
                               "Hi {{client_name}},\n\nRe {{job_title}}: I deliver "
                               "{{deliverable}}. Closest match: {{portfolio_piece}}. "
                               "{{unknown_token}}\n\nQ: {{clarifying_question}}\n"
                               "— {{your_name}}")))
    db.add(PortfolioItem(user_id=user.id, title="React SaaS Dashboard",
                         url="https://pf/1", tags=["react"]))
    db.add(RateCardEntry(user_id=user.id, skill_category="React",
                         hourly_rate=75, fixed_min=1500, currency="USD"))
    db.commit()
    job = Job(user_id=user.id, external_id="t1", platform="upwork",
              title="React dashboard build",
              description="Deliverables: responsive UI shell. " + "react typescript " * 3,
              skills=["React"], job_type="fixed", budget_min=4000, budget_max=6000)
    db.add(job)
    db.commit()

    result = await proposal_gen.generate(db, job)
    text = result["draft_text"]
    assert "{{" not in text and "}}" not in text  # no raw braces leak
    assert "Hi there," in text                    # client_name fallback
    assert "React dashboard build" in text
    assert "responsive UI shell" in text          # deliverable from analysis
    assert "React SaaS Dashboard" in text         # top matched portfolio piece
    assert "timeline" in text                     # clarifying question from missing_info
    assert result["humanized_text"]               # rendered text went through humanize
