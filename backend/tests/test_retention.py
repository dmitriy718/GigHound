"""Retention beat (retention_tick_core): hard-deletes archived jobs older
than 90 days (unless still referenced by the proposal queue), done/failed/
skipped_circuit_open stealth tasks older than 30 days, and audit_log rows
older than 365 days — all tenant-scoped. Boundaries use ±1 day around each
cutoff so the tests are not racy against the tick's internal clock.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import AuditLog, Job, ProposalQueueItem, StealthTask, User
from app.tasks import retention_tick_core


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


def _job(db, user_id, external_id, *, status="archived", age_days=100):
    job = Job(user_id=user_id, external_id=external_id, platform="upwork",
              title="t", status=status,
              fetched_at=datetime.now(timezone.utc) - timedelta(days=age_days))
    db.add(job)
    db.commit()
    return job


def test_retention_boundaries(Session, monkeypatch):
    monkeypatch.setattr("app.tasks.SessionLocal", Session)
    db = Session()
    user = _user(db, "keep@example.com")

    # jobs: archived + fetched 91d ago → deleted; 89d → kept; new status → kept
    old_archived = _job(db, user.id, "old-archived", age_days=91)
    fresh_archived = _job(db, user.id, "fresh-archived", age_days=89)
    old_new = _job(db, user.id, "old-new", status="new", age_days=200)
    # archived + 200d old BUT referenced by the proposal queue → skipped
    referenced = _job(db, user.id, "referenced", age_days=200)
    db.add(ProposalQueueItem(user_id=user.id, job_id=referenced.id, platform="upwork"))
    db.commit()

    # stealth tasks: done 31d ago → deleted; done 29d ago → kept;
    # failed 40d ago (completed_at set) → deleted; pending 40d ago → kept;
    # skipped_circuit_open 40d ago → deleted; skipped 29d ago → kept
    now = datetime.now(timezone.utc)
    t1 = StealthTask(user_id=user.id, platform="fiverr", task_type="scrape_gig_metrics",
                     status="done", created_at=now - timedelta(days=35),
                     completed_at=now - timedelta(days=31))
    t2 = StealthTask(user_id=user.id, platform="fiverr", task_type="scrape_gig_metrics",
                     status="done", created_at=now - timedelta(days=30),
                     completed_at=now - timedelta(days=29))
    t3 = StealthTask(user_id=user.id, platform="fiverr", task_type="create_gig_draft",
                     status="failed", created_at=now - timedelta(days=41),
                     completed_at=now - timedelta(days=40))
    t4 = StealthTask(user_id=user.id, platform="fiverr", task_type="scrape_gig_metrics",
                     status="pending", created_at=now - timedelta(days=40))
    t5 = StealthTask(user_id=user.id, platform="upwork", task_type="submit_upwork_proposal",
                     status="skipped_circuit_open", created_at=now - timedelta(days=40))
    t6 = StealthTask(user_id=user.id, platform="upwork", task_type="submit_upwork_proposal",
                     status="skipped_circuit_open", created_at=now - timedelta(days=29))
    db.add_all([t1, t2, t3, t4, t5, t6])
    db.commit()

    # audit log: 366d → deleted; 364d → kept
    a1 = AuditLog(user_id=user.id, action_type="proposal_approved",
                  created_at=now - timedelta(days=366))
    a2 = AuditLog(user_id=user.id, action_type="proposal_approved",
                  created_at=now - timedelta(days=364))
    db.add_all([a1, a2])
    db.commit()

    totals = retention_tick_core()
    assert totals == {"jobs_deleted": 1, "jobs_skipped_referenced": 1,
                      "stealth_tasks_deleted": 3, "audit_log_deleted": 1}

    remaining = {j.external_id for j in db.query(Job).all()}
    assert remaining == {"fresh-archived", "old-new", "referenced"}
    remaining_tasks = {t.id for t in db.query(StealthTask).all()}
    assert remaining_tasks == {t2.id, t4.id, t6.id}
    remaining_audit = {a.id for a in db.query(AuditLog).all()}
    assert remaining_audit == {a2.id}
    db.close()


def test_retention_is_tenant_scoped(Session, monkeypatch):
    monkeypatch.setattr("app.tasks.SessionLocal", Session)
    db = Session()
    alice = _user(db, "alice@example.com")
    bob = _user(db, "bob@example.com")

    # Bob's old archived job references ALICE's proposal job? No — the skip
    # check must be per-tenant: a queue item on Alice's job must not protect
    # Bob's job, and Bob's sweep must not touch Alice's rows.
    alice_job = _job(db, alice.id, "alice-old", age_days=200)
    bob_job = _job(db, bob.id, "bob-old", age_days=200)
    db.add(ProposalQueueItem(user_id=alice.id, job_id=alice_job.id, platform="upwork"))
    db.add(AuditLog(user_id=alice.id, action_type="gig_created",
                    created_at=datetime.now(timezone.utc) - timedelta(days=400)))
    db.commit()

    totals = retention_tick_core()
    # Alice's job skipped (referenced); Bob's deleted; Alice's audit row deleted
    assert totals["jobs_deleted"] == 1
    assert totals["jobs_skipped_referenced"] == 1
    assert totals["audit_log_deleted"] == 1

    assert {j.external_id for j in db.query(Job).all()} == {"alice-old"}
    db.close()
