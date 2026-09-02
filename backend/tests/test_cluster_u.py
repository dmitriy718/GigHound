"""Cluster U tests (P5-3/P5-4): Celery tick autoretry, circuit-breaker
half-open single-trial, bid caps vs client max, short-tag buyer-request
matching, own-message reply detection, digest is_active + SMTP_TLS,
scoring boundary/budget nits, atomic template wins, boolquery tokenizer
strictness, and the one-live-proposal-per-job race guard.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app import circuit_breaker
from app.auth import hash_password
from app.boolquery import BooleanQueryError, matches_boolean_query, parse_boolean_query
from app.database import Base
from app.models import (AlertSettings, GigTemplate, Job, ProposalQueueItem,
                        RateCardEntry, Template, User)

TICK_TASKS = ["fiverr_buyer_request_tick", "gig_analytics_tick", "discovery_tick",
              "outcome_sync_tick", "upwork_outcome_tick", "generation_retry_tick",
              "digest_tick", "auto_archive_tick", "follow_up_due_tick",
              "retention_tick", "stealth_reaper_tick"]


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()


@pytest.fixture()
def user(db):
    u = User(email="cluster-u@example.com",
             password_hash=hash_password("password123"), display_name="U")
    db.add(u)
    db.commit()
    return u


# ---------------- P5-3.1: tick autoretry ----------------

def test_beat_ticks_have_bounded_autoretry():
    from app import tasks
    for name in TICK_TASKS:
        t = getattr(tasks, name)
        assert t.autoretry_for == (Exception,), name
        assert t.max_retries == 3, name
        assert t.retry_backoff is True and t.retry_backoff_max == 600, name
        assert t.retry_jitter is True, name


def test_generate_proposal_task_has_no_autoretry():
    """Generation failures are retried (bounded) by generation_retry_tick —
    a second Celery-level retry path is deliberately NOT configured."""
    from app import tasks
    assert not getattr(tasks.generate_proposal_task, "autoretry_for", ())


# ---------------- P5-3.2: half-open single trial ----------------

def test_half_open_admits_exactly_one_trial(monkeypatch):
    platform = "trial-platform-a"
    circuit_breaker.open_circuit(platform, "repeated failures")
    monkeypatch.setattr(circuit_breaker, "DEFAULT_COOLDOWN_SEC", -1)
    assert circuit_breaker.is_closed(platform) is True   # trial admitted
    assert circuit_breaker.is_closed(platform) is False  # concurrent trial blocked
    assert circuit_breaker.is_closed(platform) is False


def test_trial_token_released_on_resolution(monkeypatch):
    platform = "trial-platform-b"
    circuit_breaker.open_circuit(platform, "repeated failures")
    monkeypatch.setattr(circuit_breaker, "DEFAULT_COOLDOWN_SEC", -1)
    assert circuit_breaker.is_closed(platform) is True
    assert circuit_breaker.is_closed(platform) is False
    # trial succeeded → circuit closed (releases the token); a later re-open
    # must admit a fresh trial after cooldown
    circuit_breaker.close_circuit(platform, "trial succeeded")
    circuit_breaker.open_circuit(platform, "failed again")
    assert circuit_breaker.is_closed(platform) is True


# ---------------- P5-4.1: bid caps at the client's stated max ----------------

def _job_row(db, user, **kw):
    job = Job(user_id=user.id, external_id=kw.pop("external_id", "cap-1"),
              platform=kw.pop("platform", "upwork"),
              title=kw.pop("title", "python scraper"),
              skills=kw.pop("skills", ["Python"]), **kw)
    db.add(job)
    db.commit()
    return job


def test_hourly_bid_capped_at_client_max(db, user):
    from app.proposal_gen import calculate_bid
    db.add(RateCardEntry(user_id=user.id, skill_category="python", hourly_rate=80))
    job = _job_row(db, user, job_type="hourly", budget_usd_max=15, budget_usd_min=10)
    amount, days, rationale = calculate_bid(db, job, {})
    assert amount == round(15 * 0.98, 2) and days is None
    assert "capped at client max" in rationale


def test_hourly_bid_uncapped_below_client_max(db, user):
    from app.proposal_gen import calculate_bid
    db.add(RateCardEntry(user_id=user.id, skill_category="python", hourly_rate=80))
    job = _job_row(db, user, job_type="hourly", budget_usd_max=100, budget_usd_min=50)
    amount, days, _ = calculate_bid(db, job, {})
    assert amount == 80.0 and days is None


def test_fiverr_bid_capped_at_client_max(db, user):
    from app.proposal_gen import calculate_bid
    db.add(RateCardEntry(user_id=user.id, skill_category="python",
                         hourly_rate=80, fixed_min=80))
    job = _job_row(db, user, platform="fiverr", job_type="gig",
                   budget_usd_max=15, budget_usd_min=10)
    amount, days, _ = calculate_bid(db, job, {})
    assert amount == round(15 * 0.98, 2) and days == 3


# ---------------- P5-4.2: short-tag buyer-request matching ----------------

def test_short_tag_requires_word_boundary(db, user):
    from app.fiverr_monitor import matching_buyer_requests
    db.add(GigTemplate(user_id=user.id, platform="fiverr", name="AI gigs",
                       template_json={"tags": ["ai"]}, is_active=True))
    db.commit()
    requests = [
        {"id": "1", "title": "available for detail work", "description": "", "category": ""},
        {"id": "2", "title": "Need an AI assistant built", "description": "", "category": ""},
    ]
    matched = matching_buyer_requests(db, user.id, requests)
    assert [r["id"] for r in matched] == ["2"]


# ---------------- P5-4.3: own-message reply detection ----------------

def _thread(from_user, ts, project_id="42"):
    return {"project": {"id": project_id},
            "last_message": {"from_user": from_user, "time": ts, "message": "hello"}}


def test_own_message_never_counts_as_client_reply():
    from app.outcome_sync import _client_reply
    submitted = datetime(2026, 1, 1, tzinfo=timezone.utc)
    item = SimpleNamespace(id=1, reviewed_at=submitted, created_at=submitted)
    job = SimpleNamespace(external_id="42")
    later = submitted.timestamp() + 3600
    # int bidder id vs string from_user: coercion must catch our own message
    assert _client_reply(_thread("123", later), item, 123, job) is None
    # a real client message is detected
    hit = _client_reply(_thread("456", later), item, 123, job)
    assert hit == (later, "hello")


def test_unknown_bidder_skips_reply_detection():
    from app.outcome_sync import _client_reply
    submitted = datetime(2026, 1, 1, tzinfo=timezone.utc)
    item = SimpleNamespace(id=1, reviewed_at=submitted, created_at=submitted)
    job = SimpleNamespace(external_id="42")
    later = submitted.timestamp() + 3600
    # older rows / manual submissions carry no bidder_id — skip, never guess
    assert _client_reply(_thread("123", later), item, None, job) is None


# ---------------- P5-4.4: digest gaps ----------------

def test_inactive_users_get_no_digest(db, user):
    from app.digest import due_digest_user_ids
    inactive = User(email="inactive-u@example.com",
                    password_hash=hash_password("password123"), is_active=False)
    db.add(inactive)
    db.flush()
    db.add(AlertSettings(user_id=user.id, digest_mode="hourly"))
    db.add(AlertSettings(user_id=inactive.id, digest_mode="hourly"))
    db.commit()
    assert due_digest_user_ids(db) == [user.id]


class _FakeSMTP:
    instances = []

    def __init__(self, host, port):
        self.tls = False
        _FakeSMTP.instances.append(self)

    def starttls(self):
        self.tls = True

    def login(self, user, password):
        pass

    def send_message(self, msg):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _digest_jobs():
    return [SimpleNamespace(quality_score=80.0, title="t", platform="upwork",
                            url="https://example.com/j/1",
                            budget_usd_min=None, budget_usd_max=None)]


def test_smtp_tls_false_skips_starttls(monkeypatch):
    from app.digest import send_digest_email
    _FakeSMTP.instances = []
    monkeypatch.setattr("app.digest.smtplib.SMTP", _FakeSMTP)
    monkeypatch.setenv("SMTP_HOST", "localhost")
    monkeypatch.setenv("SMTP_TLS", "false")
    assert send_digest_email(_digest_jobs(), "hourly") is True
    assert _FakeSMTP.instances[0].tls is False


def test_smtp_tls_default_starttls(monkeypatch):
    from app.digest import send_digest_email
    _FakeSMTP.instances = []
    monkeypatch.setattr("app.digest.smtplib.SMTP", _FakeSMTP)
    monkeypatch.setenv("SMTP_HOST", "localhost")
    monkeypatch.delenv("SMTP_TLS", raising=False)
    assert send_digest_email(_digest_jobs(), "hourly") is True
    assert _FakeSMTP.instances[0].tls is True


# ---------------- P5-4.6: scoring nits ----------------

def test_short_primary_keyword_requires_word_boundary():
    from app.scoring import score_keyword_match
    kw = SimpleNamespace(term="C", weight=1.0)
    # substring semantics would score on the "c" in "scale"/"client"
    pts, excluded = score_keyword_match("scale the client project", [kw], [], [])
    assert (pts, excluded) == (0.0, False)
    pts, _ = score_keyword_match("looking for a C developer", [kw], [], [])
    assert pts == 15.0


def test_budget_max_zero_is_respected():
    from app.scoring import compute_quality_score
    job = SimpleNamespace(title="free work", description="some description here",
                          budget_max=0, budget_min=None, currency="USD",
                          job_type="fixed", client_info={},
                          posted_at=None, apply_deadline=None)
    scored = compute_quality_score(job)
    # budget_max=0 must NOT fall through to budget_min (unknown): ratio 0 →
    # the unrealistic-budget floor, not the neutral 10 points
    assert scored["score_breakdown"]["budget_realism"] == 2.0
    assert "unrealistic budget" in scored["red_flags"]


# ---------------- P5-4.7: atomic template wins ----------------

def test_template_wins_survive_concurrent_updates(db, user):
    """Simulate a lost-update race: a concurrent writer bumps wins behind our
    stale ORM object's back (expire_on_commit=False keeps it stale) — the
    atomic SQL increment must not overwrite their update."""
    from app.templates import record_outcome
    tpl = Template(user_id=user.id, title="t", platform="upwork", text="x")
    db.add(tpl)
    db.commit()
    proposals = []
    for i in (1, 2):
        job = _job_row(db, user, external_id=f"tw-{i}")
        p = ProposalQueueItem(user_id=user.id, job_id=job.id, platform="upwork",
                              proposal_text="t", status="submitted",
                              template_id=tpl.id)
        db.add(p)
        db.commit()
        proposals.append(p)

    record_outcome(db, proposals[0], "hired")
    assert tpl.wins == 1
    # concurrent writer: +5 wins our in-memory tpl never saw
    db.execute(update(Template).where(Template.id == tpl.id)
               .values(wins=Template.wins + 5))
    db.commit()
    record_outcome(db, proposals[1], "hired")
    db.expire_all()
    final = db.get(Template, tpl.id)
    assert final.wins == 7  # 1 + 5 + 1 — nothing lost
    assert final.win_rate == 100.0


# ---------------- P5-4.8: tokenizer strictness ----------------

def test_tokenizer_rejects_mid_query_garbage():
    with pytest.raises(BooleanQueryError, match="unexpected input"):
        parse_boolean_query('foo " bar')
    with pytest.raises(BooleanQueryError, match="unexpected trailing input"):
        parse_boolean_query('foo bar "')
    # valid queries are untouched
    assert matches_boolean_query("(React OR Next.js) AND NOT WordPress",
                                 "react dashboard") is True


# ---------------- P5-4.9: one live proposal per job ----------------

def _queue_item(user, job, status, request_type="job"):
    return ProposalQueueItem(user_id=user.id, job_id=job.id, platform="upwork",
                             proposal_text="t", status=status,
                             request_type=request_type)


def test_live_job_unique_index(db, user):
    job = _job_row(db, user, external_id="uq-1")
    db.add(_queue_item(user, job, "rejected"))    # terminal rows pile up freely
    db.add(_queue_item(user, job, "failed"))
    db.add(_queue_item(user, job, "pending_review", request_type="follow_up"))
    db.add(_queue_item(user, job, "pending_review"))
    db.commit()
    db.add(_queue_item(user, job, "submitted"))  # second live 'job' row
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_generation_insert_race_keeps_existing(db, user):
    """Loser of the gates select-then-insert race gets the winner's row back
    instead of a duplicate insert."""
    from app.orchestrator import _commit_generation_item
    job = _job_row(db, user, external_id="uq-2")
    winner = _queue_item(user, job, "pending_review")
    db.add(winner)
    db.commit()
    loser = _queue_item(user, job, "pending_review")
    db.add(loser)
    row, committed = _commit_generation_item(db, job, loser)
    assert committed is False
    assert row.id == winner.id
    assert db.query(ProposalQueueItem).filter_by(job_id=job.id).count() == 1
