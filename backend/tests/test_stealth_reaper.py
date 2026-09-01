"""Stealth-task reaper (stealth_reaper_tick_core): a worker that dies
mid-task leaves the row `claimed` forever. Claims older than
STEALTH_CLAIM_TIMEOUT are reset to pending (reclaim_count incremented);
once reclaim_count reaches STEALTH_MAX_RECLAIMS the task is failed for good.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import StealthTask, User
from app.tasks import STEALTH_MAX_RECLAIMS, stealth_reaper_tick_core


@pytest.fixture()
def Session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def _user(db, email):
    user = User(email=email, password_hash="x")
    db.add(user)
    db.commit()
    return user


def _claimed(db, user_id, *, age_minutes=20, reclaim_count=0):
    t = StealthTask(user_id=user_id, platform="upwork",
                    task_type="submit_upwork_proposal", status="claimed",
                    claimed_by="worker-1",
                    claimed_at=(datetime.now(timezone.utc)
                                - timedelta(minutes=age_minutes)),
                    reclaim_count=reclaim_count)
    db.add(t)
    db.commit()
    return t


def test_reaper_resets_stale_claim_to_pending(Session, monkeypatch):
    monkeypatch.setattr("app.tasks.SessionLocal", Session)
    db = Session()
    user = _user(db, "reap@example.com")
    t = _claimed(db, user.id, age_minutes=20)

    result = stealth_reaper_tick_core()
    assert result == {"reclaimed": [t.id], "failed": []}

    db.refresh(t)
    assert t.status == "pending"
    assert t.reclaim_count == 1
    assert t.claimed_by is None and t.claimed_at is None
    db.close()


def test_reaper_fails_task_at_reclaim_limit(Session, monkeypatch):
    monkeypatch.setattr("app.tasks.SessionLocal", Session)
    db = Session()
    user = _user(db, "reaplimit@example.com")
    t = _claimed(db, user.id, age_minutes=20, reclaim_count=STEALTH_MAX_RECLAIMS)

    result = stealth_reaper_tick_core()
    assert result == {"reclaimed": [], "failed": [t.id]}

    db.refresh(t)
    assert t.status == "failed"
    assert t.result == {"error": "reclaim limit reached (worker died 3 times)"}
    db.close()


def test_reaper_leaves_recent_claims_alone(Session, monkeypatch):
    monkeypatch.setattr("app.tasks.SessionLocal", Session)
    db = Session()
    user = _user(db, "reapfresh@example.com")
    t = _claimed(db, user.id, age_minutes=5)

    result = stealth_reaper_tick_core()
    assert result == {"reclaimed": [], "failed": []}

    db.refresh(t)
    assert t.status == "claimed"
    assert t.reclaim_count == 0
    assert t.claimed_by == "worker-1"
    db.close()
