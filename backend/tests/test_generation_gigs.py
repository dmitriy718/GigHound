"""Tests for the proposal generation engine, anti-detection layer,
template learning, gig system, fiverr monitor, and circuit breaker."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import circuit_breaker
from app.antidetect import (build_typing_plan, humanize, inject_personality,
                            pick_opening, sentence_stats, strip_ai_tells)
from app.database import Base
from app.fiverr_monitor import (matching_buyer_requests, offers_remaining_today,
                                process_buyer_requests, queue_gig_creation)
from app.gig_analytics import (build_suggestions, competitor_price_analysis,
                               record_metrics, store_competitor_snapshot)
from app.gig_templates import (create_template, generate_faqs, seo_title_score,
                               validate_fiverr_template)
from app.auth import hash_password
from app.models import (AuditLog, Gig, GigTemplate, Job, PortfolioItem,
                        ProposalQueueItem, RateCardEntry, StealthTask, Template,
                        User)
from app.proposal_gen import (_heuristic_analysis, analyze_job, calculate_bid,
                              generate)
from app.templates import (generation_tuning, record_outcome, record_rejection,
                           save_as_template, top_templates)


@pytest.fixture(autouse=True)
def force_offline(monkeypatch):
    """These tests exercise the deterministic offline paths — never hit a
    real LLM even when Ollama is reachable on the LAN, and never share
    Redis counters with a running stack."""
    monkeypatch.setattr("app.proposal_gen.llm.llm_available", lambda: False)
    monkeypatch.setattr("app.gig_templates.llm.llm_available", lambda: False)
    monkeypatch.setattr("app.fiverr_monitor.cache._r", None)
    from app import fiverr_monitor
    fiverr_monitor._local_counters.clear()
    monkeypatch.setattr("app.circuit_breaker.cache._r", None)
    from app import circuit_breaker
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
    u = User(email="gen-test@example.com",
             password_hash=hash_password("password123"), display_name="Gen Test")
    db.add(u)
    db.commit()
    return u


@pytest.fixture()
def job(db, user):
    j = Job(user_id=user.id, external_id="pg1", platform="upwork",
            title="React dashboard for SaaS analytics platform",
            description=("We need a React and TypeScript expert. Deliverables: GraphQL API "
                         "integration, PostgreSQL schema, CI/CD pipeline, websocket real-time "
                         "updates on AWS. " * 8),
            job_type="fixed", budget_min=4000, budget_max=6000, currency="USD",
            budget_usd_min=4000, budget_usd_max=6000, skills=["React", "TypeScript"],
            quality_score=80.0, status="new")
    db.add(j)
    db.add(RateCardEntry(user_id=user.id, skill_category="React",
                         hourly_rate=75, fixed_min=1500, currency="USD"))
    db.add(PortfolioItem(user_id=user.id, title="React SaaS Dashboard", url="https://pf/1",
                         tags=["react", "typescript"]))
    db.commit()
    return j


# ---------------- anti-detection ----------------

def test_strip_banned_phrases_and_ai_tells():
    text = ("Dear Hiring Manager, I hope this finds you well. Furthermore, I can help. "
            "Moreover, I am skilled. Additionally, I am fast. 1. First point\n2. Second point")
    out = strip_ai_tells(text, "upwork")
    low = out.lower()
    assert "dear hiring manager" not in low
    assert "i hope this finds you well" not in low
    assert low.count("moreover") == 0 and low.count("additionally") == 0
    assert "1. First" not in out and "2. Second" not in out


def test_strip_banned_phrase_mid_sentence_leaves_no_artifacts():
    out = strip_ai_tells("Well, I hope this message finds you well, I can help")
    assert ",," not in out
    assert out == "Well, I can help"
    out = strip_ai_tells("I hope this finds you well. I can help")
    assert not out.startswith(".")
    assert out == "I can help"


def test_inject_personality_preserves_paragraphs():
    import random
    random.seed(0)  # deterministic marker choice
    text = ("Your project caught my eye. The scope is clear.\n\n"
            "I can start this week. The timeline works.\n\n"
            "Happy to discuss details. Looking forward to it.")
    out = inject_personality(text)
    assert out != text  # a marker was injected (only "I can start…" is eligible)
    assert out.count("\n\n") == 2  # paragraph boundaries survive


def test_sentence_stats_distribution():
    text = ("Short one. Also short. Tiny. This is a medium length sentence with several words in it. "
            "This particular sentence is deliberately made very long indeed so that it clearly "
            "exceeds the twenty word threshold used by the stats collector function.")
    stats = sentence_stats(text)
    assert stats["total"] == 5
    assert stats["short_pct"] == 60 and stats["long_pct"] == 20


def test_opening_rotation_no_placeholders_left():
    opening = pick_opening(title="React app", tech="React")
    assert "{title}" not in opening and "{tech}" not in opening


def test_typing_plan_ops():
    text = "the project and the timeline work for the budget you posted"
    plan = build_typing_plan(text, seed=42)
    assert 1 <= len(plan) <= 3
    for op in plan:
        assert op["typo"] != op["word"]
        assert text.split()[op["word_index"]].strip(".,;:!?") == op["word"]


def test_humanize_full_pass():
    result = humanize("I am excited to apply. Furthermore, here is my plan. "
                      "The work is clear. I can deliver.", platform="upwork",
                      title="React app", tech="React")
    assert "i am excited" not in result["humanized_text"].lower()
    assert result["typing_plan"]  # non-empty plan
    assert result["raw_text"] != result["humanized_text"]


# ---------------- proposal generation ----------------

def test_heuristic_analysis(job):
    analysis = _heuristic_analysis(job)
    assert "react" in [s.lower() for s in analysis["required_skills"]]
    assert analysis["budget_mentioned"] is True
    assert analysis["tone"] in ("professional", "casual", "technical")


@pytest.mark.asyncio
async def test_analyze_job_offline(job):
    analysis, meta = await analyze_job(job)
    assert meta["model"] == "heuristic-offline"  # no LLM_API_KEY in tests
    assert "required_skills" in analysis


def test_bid_calculation_fixed(db, job):
    amount, days, rationale = calculate_bid(db, job, {"required_skills": ["React"]})
    assert amount and amount > 0
    assert days and 3 <= days <= 60
    assert "×" in rationale and "complexity" in rationale


def test_bid_calculation_hourly(db, job):
    job.job_type = "hourly"
    amount, days, rationale = calculate_bid(db, job, {})
    assert amount == 75.0 and days is None


@pytest.mark.asyncio
async def test_generate_offline_pipeline(db, job):
    result = await generate(db, job)
    assert result["draft_text"] and result["humanized_text"]
    assert result["bid_amount"] is not None
    assert result["portfolio_item_ids"]  # portfolio auto-selected
    assert 0 <= result["confidence"] <= 100
    assert result["analysis"]["required_skills"]
    assert result["latency_ms"] >= 0
    # upwork profile: no numbered lists, contains a question
    assert "?" in result["draft_text"]


@pytest.mark.asyncio
async def test_generate_fiverr_is_ultra_brief(db, job):
    job.platform = "fiverr"
    result = await generate(db, job)
    assert len(result["draft_text"].split()) <= 60


# ---------------- template learning ----------------

def test_template_win_rate_lifecycle(db, user, job):
    item = ProposalQueueItem(user_id=user.id, job_id=job.id, platform="upwork",
                             proposal_text="winning text", bid_amount=5000,
                             status="approved")
    db.add(item)
    db.commit()
    tpl = save_as_template(db, item, title="React Dashboard", tags=["react"])
    item.template_id = tpl.id
    db.commit()
    record_outcome(db, item, "hired")
    db.refresh(tpl)
    assert tpl.wins == 1 and tpl.win_rate == 100.0
    record_outcome(db, item, "rejected")
    db.refresh(tpl)
    assert tpl.win_rate == 50.0


def test_rejection_learning_adjusts_temperature(db, user, job):
    item = ProposalQueueItem(user_id=user.id, job_id=job.id, platform="upwork",
                             proposal_text="x", status="rejected")
    db.add(item)
    db.commit()
    for _ in range(3):
        record_rejection(db, item, "too_generic")
    tuning = generation_tuning(db, user.id, "upwork")
    assert tuning["temperature"] < 0.7
    assert any("job-specific" in h for h in tuning["prompt_hints"])


def test_top_templates_ranking(db, user):
    db.add_all([
        Template(user_id=user.id, title="A", platform="upwork", text="t", tags=["react"],
                 uses=5, wins=4, losses=1, win_rate=80.0),
        Template(user_id=user.id, title="B", platform="upwork", text="t", tags=["logo"],
                 uses=5, wins=1, losses=4, win_rate=20.0),
    ])
    db.commit()
    top = top_templates(db, user.id, "upwork", ["react"])
    assert top[0].title == "A"


# ---------------- gig templates ----------------

GOOD_FIVERR = {
    "title": "I will build a React dashboard for your SaaS analytics platform",
    "category": "Programming & Tech", "subcategory": "Website Development",
    "tags": ["react", "dashboard", "saas"],
    "pricing": {"basic": {"price": 50, "delivery_days": 3, "revisions": 1}},
    "description": {"hook": "h", "what_you_get": "w", "why_me": "m", "cta": "c"},
}


def test_fiverr_template_validation(db, user):
    tpl, problems = create_template(db, user.id, "fiverr", "React Gig", GOOD_FIVERR)
    assert problems == [] and tpl.id
    bad = {**GOOD_FIVERR, "title": "x" * 100, "tags": ["a"] * 6}
    tpl2, problems2 = create_template(db, user.id, "fiverr", "Bad", bad)
    assert tpl2 is None and any("80 chars" in p for p in problems2) and any("tags" in p for p in problems2)


def test_seo_title_score():
    good = seo_title_score("I will build a React dashboard for your SaaS analytics platform",
                           ["react", "dashboard"])
    assert good["score"] >= 90
    bad = seo_title_score("Best cheap logos!!!", ["react"])
    assert bad["score"] < 60 and bad["issues"]


@pytest.mark.asyncio
async def test_faq_fallback():
    faqs = await generate_faqs("web development", "I will build your website")
    assert 3 <= len(faqs) <= 5
    assert all("question" in f and "answer" in f for f in faqs)


# ---------------- fiverr monitor + circuit breaker ----------------

def _fiverr_template(db, user_id):
    tpl, problems = create_template(db, user_id, "fiverr", "React Gig", GOOD_FIVERR)
    assert not problems
    return tpl


def test_gig_creation_queue_draft_only(db, user):
    tpl = _fiverr_template(db, user.id)
    task, err = queue_gig_creation(db, tpl)
    assert err == "" and task.task_type == "create_gig_draft"
    assert task.payload["save_as_draft"] is True
    # 1 draft/hour cap
    task2, err2 = queue_gig_creation(db, tpl)
    assert task2 is None and "rate limit" in err2
    # audit written
    assert db.query(AuditLog).filter_by(action_type="gig_created").count() == 1


def test_gig_creation_blocked_by_open_circuit(db, user):
    circuit_breaker.open_circuit("fiverr", "test")
    tpl = _fiverr_template(db, user.id)
    task, err = queue_gig_creation(db, tpl)
    assert task is None and "circuit OPEN" in err
    circuit_breaker.close_circuit("fiverr")


def test_gig_draft_cap_is_per_user(db, user):
    """The 1 draft/hour cap is per tenant — another user keeps drafting."""
    tpl_a = _fiverr_template(db, user.id)
    task, err = queue_gig_creation(db, tpl_a)
    assert err == ""
    task2, err2 = queue_gig_creation(db, tpl_a)
    assert task2 is None and "rate limit" in err2

    other = User(email="gen-test-2@example.com",
                 password_hash=hash_password("password123"))
    db.add(other)
    db.commit()
    tpl_b = _fiverr_template(db, other.id)
    task_b, err_b = queue_gig_creation(db, tpl_b)
    assert err_b == "" and task_b is not None


def test_buyer_request_processing(db, user):
    _fiverr_template(db, user.id)
    requests = [
        {"id": "br1", "title": "Need a React website developer", "budget": 120,
         "description": "website development for my SaaS"},
        {"id": "br2", "title": "Voice over for audiobook", "budget": 80,
         "description": "narration work"},
    ]
    result = process_buyer_requests(db, user.id, requests)
    assert result["queued"] == 1  # only the React one matches template tags
    item = db.query(ProposalQueueItem).filter_by(request_type="buyer_request").one()
    assert item.status == "pending_review"  # approval always required
    assert "$120" in item.proposal_text or item.bid_amount == 120
    assert offers_remaining_today(user.id) == 9
    # duplicate request not re-queued
    result2 = process_buyer_requests(db, user.id, requests)
    assert result2["queued"] == 0


def test_matching_buyer_requests_no_templates(db, user):
    assert matching_buyer_requests(db, user.id, [{"title": "react dev"}]) == []


# ---------------- gig analytics ----------------

def test_suggestions_rules():
    s = build_suggestions(impressions=50, clicks=1, orders=0)
    assert any(x["area"] == "title_keywords" for x in s)
    s2 = build_suggestions(impressions=1000, clicks=10, orders=0)
    assert any(x["area"] == "thumbnail" for x in s2)
    s3 = build_suggestions(impressions=1000, clicks=100, orders=1)
    assert any(x["area"] == "pricing_or_description" for x in s3)
    assert build_suggestions(impressions=1000, clicks=100, orders=20) == []


def test_competitor_price_analysis():
    gigs = [{"price": 50}, {"price": 60}, {"price": 70}]
    insights = competitor_price_analysis(gigs, my_price=100)
    assert any("above market" in i for i in insights)
    insights2 = competitor_price_analysis(gigs, my_price=30)
    assert any("below market" in i for i in insights2)


def test_record_metrics_and_snapshot(db, user):
    gig = Gig(user_id=user.id, platform="fiverr", title="React gig", status="active")
    db.add(gig)
    db.commit()
    m = record_metrics(db, gig, impressions=80, clicks=2, orders=0, revenue=0)
    assert m.week and any(s["area"] == "title_keywords" for s in m.suggestions)
    snap = store_competitor_snapshot(db, user.id, "fiverr", "Website Development",
                                     [{"price": 50, "title": "x"}], my_price=80)
    assert snap.insights
