"""Rate governor tests (Phase 4.5): shared limiter registry, pacing jitter,
daily action budgets. All offline — Redis is faked, no network."""
import asyncio
import random

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.adapters import ratelimit
from app.adapters.freelancer import FreelancerAdapter
from app.adapters.linkedin import LinkedInJobsAdapter
from app.adapters.ratelimit import (AsyncRateLimiter, DailyBudgetExceeded,
                                    consume_daily_action, daily_cap,
                                    get_limiter)
from app.adapters.upwork_agency import UpworkAgencyAdapter
from app.auth import hash_password
from app.database import Base
from app.models import User


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture()
def user(db):
    u = User(email="ratelimit-test@example.com",
             password_hash=hash_password("password123"), display_name="Rate Test")
    db.add(u)
    db.commit()
    return u


class _FakeRedis:
    def __init__(self):
        self.store = {}

    def incr(self, key):
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    def expire(self, key, ttl):
        pass


# ---------------- shared limiter registry ----------------

def test_limiter_shared_per_platform_principal():
    a = get_limiter("freelancer", "user1:default", 2.0)
    b = get_limiter("freelancer", "user1:default", 2.0)
    c = get_limiter("freelancer", "user2:default", 2.0)
    d = get_limiter("upwork", "user1:default", 8.0)
    assert a is b
    assert a is not c  # per-principal isolation
    assert a is not d  # per-platform isolation


def test_adapters_thread_principal_and_share_limiter(db, user):
    a1 = FreelancerAdapter(db, user.id)
    a2 = FreelancerAdapter(db, user.id)
    assert a1.principal == f"user{user.id}:default"
    assert a1.limiter is a2.limiter  # pacing survives per-request instances

    up = UpworkAgencyAdapter(db, user.id)
    assert up.principal == f"user{user.id}:agency_manager"
    assert up.limiter is not a1.limiter

    li = LinkedInJobsAdapter(db, user_id=user.id)
    assert li.principal == f"user{user.id}:default"
    anon = LinkedInJobsAdapter(db)
    assert anon.principal == "default"


# ---------------- jitter ----------------

@pytest.mark.asyncio
async def test_acquire_jitters_interval(monkeypatch):
    calls = []
    real_uniform = random.uniform

    def spy_uniform(a, b):
        calls.append((a, b))
        return real_uniform(a, b)

    monkeypatch.setattr(ratelimit.random, "uniform", spy_uniform)
    limiter = AsyncRateLimiter(2.0)  # interval 0.5s
    await limiter.acquire()
    limiter.release()
    assert calls[0] == (0.7, 1.3)  # ±30% jitter


@pytest.mark.asyncio
async def test_shared_limiter_paces_within_loop(monkeypatch):
    monkeypatch.setattr(ratelimit.random, "uniform", lambda a, b: 1.0)  # exact 0.5s
    limiter = get_limiter("test-platform", "test-principal", 2.0)
    loop = asyncio.get_running_loop()
    await limiter.acquire()
    limiter.release()
    start = loop.time()
    await limiter.acquire()
    limiter.release()
    assert loop.time() - start >= 0.49


def test_limiter_survives_across_event_loops():
    limiter = AsyncRateLimiter(100.0)

    async def use():
        await limiter.acquire()
        limiter.release()

    asyncio.run(use())
    asyncio.run(use())  # second loop — no "attached to a different loop"


# ---------------- daily action budget ----------------

def test_daily_cap_parsing(monkeypatch):
    assert daily_cap("linkedin") is None  # unset = unlimited
    monkeypatch.setenv("GIGHOUND_DAILY_CAP_LINKEDIN", "30")
    assert daily_cap("linkedin") == 30
    monkeypatch.setenv("GIGHOUND_DAILY_CAP_LINKEDIN", "0")
    assert daily_cap("linkedin") is None
    monkeypatch.setenv("GIGHOUND_DAILY_CAP_LINKEDIN", "junk")
    assert daily_cap("linkedin") is None


def test_consume_daily_action_enforces_cap(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(ratelimit.cache, "_r", fake)
    monkeypatch.setenv("GIGHOUND_DAILY_CAP_LINKEDIN", "2")
    assert consume_daily_action("linkedin", "user1:default") == 1
    assert consume_daily_action("linkedin", "user1:default") == 2
    with pytest.raises(DailyBudgetExceeded, match="daily action budget"):
        consume_daily_action("linkedin", "user1:default")
    # principals are isolated
    assert consume_daily_action("linkedin", "user2:default") == 1
    key = next(iter(fake.store))
    assert key.startswith("rl:linkedin:user1:default:")


def test_consume_daily_action_noop_without_cap_or_redis(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(ratelimit.cache, "_r", fake)
    monkeypatch.delenv("GIGHOUND_DAILY_CAP_LINKEDIN", raising=False)
    assert consume_daily_action("linkedin", "user1:default") == 0  # no cap
    assert fake.store == {}
    monkeypatch.setenv("GIGHOUND_DAILY_CAP_LINKEDIN", "1")
    monkeypatch.setattr(ratelimit.cache, "_r", None)  # Redis down
    assert consume_daily_action("linkedin", "user1:default") == 0  # graceful no-op
