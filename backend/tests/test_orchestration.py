"""Tests for scoring v2, boolean query parser, and the orchestration pipeline."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth import hash_password
from app.boolquery import BooleanQueryError, matches_boolean_query, parse_boolean_query
from app.database import Base
from app.models import (Keyword, KeywordGroup, PortfolioItem, ProfileTemplate,
                        ProposalQueueItem, RateCardEntry, SearchFilter,
                        SearchProfile, Job, User)
from app.orchestrator import maybe_queue_proposal, select_portfolio_items
from app.schemas import ClientInfo, JobIngest
from app.scoring import compute_quality_score


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def user(db):
    u = User(email="orch-test@example.com",
             password_hash=hash_password("password123"), display_name="Orch Test")
    db.add(u)
    db.commit()
    return u


class KW:
    def __init__(self, term, kind, weight=1.0):
        self.term, self.kind, self.weight = term, kind, weight


KWS = [KW("react", "primary", 1.0), KW("typescript", "primary", 0.8),
       KW("frontend", "secondary"), KW("wordpress", "negative")]

GOOD_DESC = (
    "We need a React and TypeScript expert to build an analytics dashboard. "
    "Deliverables: responsive UI, GraphQL API integration, PostgreSQL schema, CI/CD pipeline. "
    "Scope of work includes websocket real-time updates and deployment on AWS. " * 8
)


def _job(**kw):
    base = dict(
        external_id="j1", platform="upwork",
        title="Senior React/TypeScript developer for SaaS dashboard",
        description=GOOD_DESC, url="https://x", job_type="fixed",
        budget_min=4000, budget_max=6000, currency="USD", experience_level="expert",
        client_info=ClientInfo(payment_verified=True, identity_verified=True,
                               past_hires=40, total_spent=50000, rating=4.9),
        proposals_count=3, skills=["React", "TypeScript"], languages=["English"],
        work_arrangement="remote",
        posted_at=datetime.now(timezone.utc) - timedelta(minutes=30),
    )
    base.update(kw)
    return JobIngest(**base)


# ---------------- Scoring v2 ----------------

def test_good_job_scores_high():
    r = compute_quality_score(_job(), KWS, market_rate=75.0)
    bd = r["score_breakdown"]
    assert r["quality_score"] >= 60
    assert bd["client_verification"] == 18.0  # payment +9, identity +5, hires 40→+4
    assert bd["description_quality"] == 20.0  # long + deliverables + tech reqs
    assert set(bd) == {"keyword_match", "budget_realism", "client_verification",
                       "description_quality", "urgency_ratio", "red_flag_penalty"}


def test_red_flags_minus_30_each_capped():
    r = compute_quality_score(_job(
        description="unlimited revisions, test task before hire, work for exposure. " + GOOD_DESC,
        budget_min=100, budget_max=150,
    ))
    assert r["score_breakdown"]["red_flag_penalty"] == -60.0  # 3 flags, capped
    for flag in ("unlimited revisions", "test task before hire", "work for exposure/review"):
        assert flag in r["red_flags"]


def test_urgent_low_budget_flag():
    r = compute_quality_score(_job(
        title="URGENT: need fix today", description="urgent asap react fix needed today",
        budget_min=50, budget_max=100,
    ))
    assert "urgent + low budget" in r["red_flags"]


def test_student_project_flag():
    r = compute_quality_score(_job(description="This is a student project for my homework. " + GOOD_DESC))
    assert "student/budget project" in r["red_flags"]


def test_vague_description_penalty():
    r = compute_quality_score(_job(
        title="Need a website", description="need a website, quick job, easy task, help me asap",
        budget_min=None, budget_max=None, client_info=ClientInfo(),
    ))
    assert r["score_breakdown"]["description_quality"] == 0.0


def test_negative_keyword_excludes():
    r = compute_quality_score(_job(description="wordpress react site " + GOOD_DESC), KWS)
    assert r["quality_score"] == 0.0
    assert "negative keyword match" in r["red_flags"]


def test_budget_realism_hourly_uses_rate():
    r = compute_quality_score(_job(job_type="hourly", budget_min=10, budget_max=15),
                              KWS, market_rate=75.0)
    assert r["score_breakdown"]["budget_realism"] == 2.0  # $15/h vs $75 market → unrealistic


# ---------------- Boolean parser ----------------

@pytest.mark.parametrize("query,text,expected", [
    ("(React OR Next.js) AND (NOT WordPress)", "react developer needed", True),
    ("(React OR Next.js) AND (NOT WordPress)", "next.js developer needed", True),
    ("(React OR Next.js) AND (NOT WordPress)", "wordpress react site", False),
    ("(React OR Next.js) AND (NOT WordPress)", "django backend", False),
    ("Python AND (ML OR LLM)", "python llm engineer", True),
    ("Python AND (ML OR LLM)", "python django", False),
    ('"machine learning" AND NOT intern', "machine learning role", True),
    ('"machine learning" AND NOT intern', "machine learning intern", False),
    ("react typescript", "react and typescript dev", True),  # implicit AND
    ("", "anything", True),
])
def test_boolean_queries(query, text, expected):
    assert matches_boolean_query(query, text) is expected


def test_boolean_syntax_error():
    with pytest.raises(BooleanQueryError):
        parse_boolean_query("(React OR")


# ---------------- Orchestrator ----------------

def _seed_profile_assets(db, user_id):
    db.add(ProfileTemplate(user_id=user_id, platform="upwork", name="Default",
                           pitch_template="Hi {{client_name}}, about \"{{job_title}}\": "
                                          "I deliver {{deliverable}}. See {{portfolio_piece}}. "
                                          "Rate: {{rate_line}} — {{your_name}}"))
    db.add(PortfolioItem(user_id=user_id, title="React SaaS Dashboard", url="https://pf/1",
                         tags=["react", "typescript"]))
    db.add(PortfolioItem(user_id=user_id, title="Logo Pack", url="https://pf/2",
                         tags=["logo", "branding"]))
    db.add(RateCardEntry(user_id=user_id, skill_category="React",
                         hourly_rate=75, fixed_min=1500, currency="USD"))
    db.commit()


def test_portfolio_auto_select(db, user):
    _seed_profile_assets(db, user.id)
    job = Job(user_id=user.id, external_id="x", platform="upwork", title="React dashboard",
              skills=["React", "TypeScript"])
    selected = select_portfolio_items(db, user.id, job)
    assert [p.title for p in selected] == ["React SaaS Dashboard"]


@pytest.mark.asyncio
async def test_maybe_queue_proposal_pipeline(db, user):
    _seed_profile_assets(db, user.id)
    db.add(SearchProfile(user_id=user.id, name="react only",
                         boolean_query="(React OR Next.js) AND (NOT WordPress)",
                         auto_queue_proposals=True))
    good = Job(user_id=user.id, external_id="g", platform="upwork", title="React app",
               skills=["React"], description="react work", status="new", job_type="fixed")
    bad = Job(user_id=user.id, external_id="b", platform="upwork", title="WordPress site",
              skills=["WordPress"], description="wordpress work", status="new", job_type="fixed")
    db.add_all([good, bad])
    db.commit()

    item = await maybe_queue_proposal(db, good)
    assert item is not None and item.status == "pending_review"
    # boolean query excludes the WordPress job
    assert await maybe_queue_proposal(db, bad) is None
    # no double-queueing
    assert await maybe_queue_proposal(db, good) is None
    assert db.query(ProposalQueueItem).count() == 1
    # archived jobs never queue
    good.status = "archived"
    db.commit()
    assert await maybe_queue_proposal(db, good) is None
