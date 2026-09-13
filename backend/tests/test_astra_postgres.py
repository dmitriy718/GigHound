"""Opt-in real-Postgres races; each test owns a unique disposable schema."""
import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import os
from threading import Event
from uuid import uuid4

from fastapi import HTTPException
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import AuditLog, Job, ProposalQueueItem, Template, User, PlatformAccount


@pytest.fixture
def pg_sessions():
    url = os.environ.get('GIGHOUND_TEST_POSTGRES_URL')
    if not url:
        pytest.skip('Set GIGHOUND_TEST_POSTGRES_URL for isolated PostgreSQL race tests')
    schema = 'astra_test_' + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA {schema}'))
    engine = create_engine(url, connect_args={'options': f'-csearch_path={schema}'})
    try:
        Base.metadata.create_all(engine)
        yield sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        admin.dispose()


def seed(sessions, platform='guru', status='pending_review'):
    with sessions() as db:
        user = User(email='concurrency@example.test', password_hash='unused')
        db.add(user); db.flush()
        db.add(PlatformAccount(user_id=user.id, platform=platform, label='Synthetic account', principal='default', mode='hybrid', enabled=True)); db.flush()
        job = Job(user_id=user.id, platform=platform, external_id='123', title='Test')
        db.add(job); db.flush()
        item = ProposalQueueItem(user_id=user.id, job_id=job.id, platform=platform,
                                 status=status, proposal_text='Approved text', bid_amount=125,
                                 submission_result={'bidder_id': 777})
        db.add(item); db.commit()
        return user.id, item.id


def test_review_lock_serializes_approval_and_rejection(pg_sessions, monkeypatch):
    from app import templates
    from app.routers.proposals import approve_proposal, reject_proposal
    from app.schemas import ProposalReviewAction, ProposalRejectAction
    uid, pid = seed(pg_sessions)
    locked, release, rejecting = Event(), Event(), Event()
    original = templates.template_for_approval
    def hold(db, item):
        result = original(db, item)
        locked.set()
        assert release.wait(10)
        return result
    monkeypatch.setattr(templates, 'template_for_approval', hold)
    def approve():
        with pg_sessions() as db:
            return approve_proposal(pid, ProposalReviewAction(expected_revision=1, reviewer='reviewer', save_as_template=True), db, db.get(User, uid))
    def reject():
        with pg_sessions() as db:
            user = db.get(User, uid)
            rejecting.set()
            return reject_proposal(pid, ProposalRejectAction(reviewer='other', reason='other'), db, user)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(approve)
        assert locked.wait(10)
        second = pool.submit(reject)
        try:
            assert rejecting.wait(10)
            with pytest.raises(TimeoutError):
                second.result(timeout=0.2)
        finally:
            release.set()
        assert first.result(timeout=10).status == 'approved'
        with pytest.raises(HTTPException) as exc:
            second.result(timeout=10)
        assert exc.value.status_code == 409
    with pg_sessions() as db:
        assert db.get(ProposalQueueItem, pid).status == 'approved'
        assert db.query(Template).count() == 1
        assert db.query(AuditLog).filter_by(action_type='proposal_approved').count() == 1


def test_legacy_and_main_submission_share_one_claim(pg_sessions, monkeypatch):
    from app.routers.proposals import submit_proposal
    from app.routers.adapters import freelancer_bid, QueueItemAction
    uid, pid = seed(pg_sessions, platform='freelancer', status='approved')
    writing, release = Event(), Event()
    calls = []
    class Adapter:
        def __init__(self, *args, **kwargs): pass
        async def place_bid(self, **kwargs):
            calls.append(kwargs)
            writing.set()
            assert release.wait(10)
            return {'id': 'receipt-123'}
        async def close(self): pass
        def bids_remaining(self): return 10
    monkeypatch.setattr('app.adapters.freelancer.FreelancerAdapter', Adapter)
    monkeypatch.setattr('app.routers.adapters.FreelancerAdapter', Adapter)
    def main():
        with pg_sessions() as db:
            return asyncio.run(submit_proposal(pid, db, db.get(User, uid)))
    def legacy():
        with pg_sessions() as db:
            return asyncio.run(freelancer_bid(QueueItemAction(proposal_queue_item_id=pid), db, db.get(User, uid)))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(main)
        assert writing.wait(10)
        try:
            second = pool.submit(legacy)
            with pytest.raises(HTTPException) as exc:
                second.result(timeout=10)
            assert exc.value.status_code == 409
        finally:
            release.set()
        assert first.result(timeout=10).status == 'submitted'
    assert len(calls) == 1
    assert calls[0]['proposal'] == 'Approved text'


def test_daily_send_budget_serializes_competing_tasks(pg_sessions, monkeypatch):
    from app.models import StealthTask, AuthTransaction
    from app.send_budget import reserve_send
    from threading import Barrier
    uid, _ = seed(pg_sessions)
    with pg_sessions() as db:
        tasks = [StealthTask(user_id=uid, platform='fiverr', task_type='submit_fiverr_offer', payload={}) for _ in range(2)]
        db.add_all(tasks); db.commit(); ids = [t.id for t in tasks]
    monkeypatch.setenv('GIGHOUND_DAILY_SUBMIT_CAP_FIVERR','1')
    ready = Barrier(2)
    def reserve(task_id):
        with pg_sessions() as db:
            task = db.get(StealthTask, task_id)
            ready.wait(timeout=5)
            try:
                reserve_send(db, task); db.commit(); return 'reserved'
            except HTTPException as exc:
                db.rollback(); return exc.status_code
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, ids))
    assert sorted(map(str, results)) == ['409', 'reserved']
    with pg_sessions() as db:
        assert db.query(AuthTransaction).filter_by(kind='send_attempt').count() == 1


def test_tenant_status_time_index_is_used_on_representative_fixture(pg_sessions):
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import insert
    uid, _ = seed(pg_sessions)
    with pg_sessions() as db:
        now = datetime.now(timezone.utc)
        tenants = [User(email=f"load-{n}@example.test",password_hash="unused") for n in range(99)]
        db.add_all(tenants); db.flush()
        user_ids = [uid] + [u.id for u in tenants]
        db.execute(insert(Job), [dict(user_id=user_ids[n%100], platform='guru', external_id=f'plan-{n}',
                                     title='Synthetic index fixture', status='new' if (n//100)%2==0 else 'archived',
                                     fetched_at=now-timedelta(minutes=n)) for n in range(10000)])
        db.commit()
        db.execute(text('ANALYZE jobs'))
        plan = db.execute(text("EXPLAIN (ANALYZE, FORMAT JSON) SELECT id FROM jobs WHERE user_id=:uid AND status='new' ORDER BY fetched_at LIMIT 20"), {'uid':uid}).scalar()
        assert 'ix_jobs_tenant_status_fetched' in str(plan)
        assert plan[0]['Plan']['Actual Rows'] == 20


def test_monthly_bid_allowance_is_atomic_and_principal_scoped(pg_sessions):
    from app.adapters.freelancer import FreelancerAdapter
    from app.adapters.base import QuotaDepletedError
    from threading import Barrier
    uid, _ = seed(pg_sessions)
    ready = Barrier(2)
    def reserve(_):
        with pg_sessions() as db:
            adapter = FreelancerAdapter(db, uid, principal='first', monthly_bid_quota=1)
            try:
                ready.wait(timeout=5)
                adapter._reserve_monthly_bid()
                return 'reserved'
            except QuotaDepletedError:
                return 'exhausted'
            finally:
                asyncio.run(adapter.close())
    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(reserve, range(2))) == ['exhausted', 'reserved']
    with pg_sessions() as db:
        adapter = FreelancerAdapter(db, uid, principal='second', monthly_bid_quota=1)
        try:
            adapter._reserve_monthly_bid()
            assert adapter.bids_remaining() == 0
        finally:
            asyncio.run(adapter.close())


@pytest.mark.parametrize('mode', ['refresh', 'enrollment'])
def test_late_vault_write_cannot_restore_deleted_account_credentials(pg_sessions, mode):
    from app.adapters.vault import CredentialVault
    from app.adapters.base import AdapterAuthError
    from app.routers.orchestration import delete_account
    uid, _ = seed(pg_sessions, platform='freelancer')
    with pg_sessions() as db:
        account = db.query(PlatformAccount).filter_by(user_id=uid).one()
        aid = account.id
        CredentialVault(db, uid, account_id=aid).store('freelancer','default',{'access_token':'synthetic-old'})
    observed, deleted = Event(), Event()
    def late_write():
        with pg_sessions() as db:
            vault = CredentialVault(db, uid, account_id=aid if mode == 'enrollment' else None)
            if mode == 'refresh': vault.load('freelancer','default')
            observed.set()
            assert deleted.wait(10)
            with pytest.raises(AdapterAuthError):
                vault.store('freelancer','default',{'access_token':'synthetic-late'})
            db.rollback()
    with ThreadPoolExecutor(max_workers=1) as pool:
        writer = pool.submit(late_write)
        assert observed.wait(10)
        try:
            with pg_sessions() as db:
                delete_account(aid, db, db.get(User,uid))
        finally:
            deleted.set()
        writer.result(timeout=10)
    with pg_sessions() as db:
        assert CredentialVault(db,uid).load('freelancer','default') is None


def test_sql_half_open_trial_admission_survives_process_replacement(pg_sessions, monkeypatch):
    from app import circuit_breaker as cb
    from threading import Barrier
    monkeypatch.setattr(cb, 'SessionLocal', pg_sessions)
    cb.transition('upwork','half_open')
    cb._local.clear(); cb._local_trials.clear()
    ready=Barrier(2)
    def acquire(_):
        with pg_sessions() as db:
            ready.wait(timeout=5)
            result=cb.is_closed('upwork', db=db)
            db.commit()
            return result
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(acquire,range(2))) == [False,True]
    assert cb.get_state('upwork')['state'] == 'half_open'
    assert not cb.is_closed('upwork')


def test_trial_resolution_rollback_never_publishes_closed_state(pg_sessions, monkeypatch):
    from app import circuit_breaker as cb
    monkeypatch.setattr(cb,'SessionLocal',pg_sessions)
    cb.open_circuit('fiverr','automatic failure')
    with pg_sessions() as transaction:
        cb.close_circuit('fiverr','uncommitted',db=transaction)
        assert cb.get_state('fiverr')['state']=='open'
        transaction.rollback()
    assert cb.get_state('fiverr')['state']=='open'


def test_concurrent_roster_additions_preserve_both_members(pg_sessions):
    from threading import Barrier
    from app.adapters.upwork_agency import UpworkAgencyAdapter
    uid,_=seed(pg_sessions,platform='upwork')
    ready=Barrier(2)
    def add(name):
        with pg_sessions() as db:
            adapter=UpworkAgencyAdapter(db,uid,principal='default')
            try:
                ready.wait(timeout=5)
                adapter.add_agency_member(name)
            finally:
                asyncio.run(adapter.close())
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(add,['alice','bob']))
    with pg_sessions() as db:
        adapter=UpworkAgencyAdapter(db,uid,principal='default')
        try:
            assert {m['username'] for m in adapter.list_agency_members()}=={'alice','bob'}
        finally:
            asyncio.run(adapter.close())


def test_duplicate_reply_polling_emits_one_notification(pg_sessions,monkeypatch):
    from threading import Barrier
    from datetime import datetime,timedelta,timezone
    from app.outcome_sync import _sync_account_threads
    uid,pid=seed(pg_sessions,platform='freelancer',status='submitted')
    with pg_sessions() as db:
        item=db.get(ProposalQueueItem,pid)
        item.submitted_at=datetime.now(timezone.utc)-timedelta(hours=1)
        item.submission_result={'bidder_id':111}
        job=db.get(Job,item.job_id);job.client_info={'client_id':'222'}
        db.commit(); external_id=job.external_id
    ready=Barrier(2); events=[]
    async def broadcast(uid,event): events.append(event)
    monkeypatch.setattr('app.outcome_sync.alerts.broadcast',broadcast)
    class Adapter:
        async def get_threads(self,**kw):
            ready.wait(timeout=5)
            return [{'project_id':external_id,'last_message':{'from_user':222,'time':datetime.now(timezone.utc).timestamp()-10,'message':'Synthetic reply'}}]
    def sync(_):
        with pg_sessions() as db:
            accounts={a.id:a for a in db.query(PlatformAccount).filter_by(user_id=uid).all()}
            return asyncio.run(_sync_account_threads(db,db.get(User,uid),Adapter(),'default',accounts))[0]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results=list(executor.map(sync,range(2)))
    assert sorted(results)==[0,1]
    assert len(events)==1


def test_gig_seller_assignment_compare_and_set(pg_sessions):
    from threading import Barrier
    from app.models import Gig
    from app.routers.gigs import assign_gig_account
    from app.schemas import GigAccountIn
    uid, _ = seed(pg_sessions, platform='fiverr')
    with pg_sessions() as db:
        first = db.query(PlatformAccount).filter_by(user_id=uid).one()
        second = PlatformAccount(user_id=uid, platform='fiverr', label='Second', principal='second', mode='stealth', enabled=True)
        gig = Gig(user_id=uid, platform='fiverr', title='Synthetic race')
        db.add_all([second, gig]); db.commit()
        ids, gid = [first.id, second.id], gig.id
    ready = Barrier(2)
    def assign(account_id):
        with pg_sessions() as db:
            user = db.get(User, uid)
            ready.wait(timeout=5)
            try:
                result = assign_gig_account(gid, GigAccountIn(account_id=account_id, expected_version=0), db, user)
                return (200, result.account_id)
            except HTTPException as exc:
                return (exc.status_code, account_id)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(assign, ids))
    assert sorted(status for status, _ in results) == [200, 409]
    with pg_sessions() as db:
        gig = db.get(Gig, gid)
        assert gig.account_binding_version == 1
        assert gig.account_id == next(account_id for status, account_id in results if status == 200)
