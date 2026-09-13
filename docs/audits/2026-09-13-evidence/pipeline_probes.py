import os, asyncio, tempfile
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from cryptography.fernet import Fernet
os.environ.update(DATABASE_URL='sqlite://',REDIS_URL='redis://127.0.0.1:1/15',GIGHOUND_SECRET_KEY='audit-synthetic-only',GIGHOUND_WORKER_TOKEN='audit-synthetic-only',GIGHOUND_VAULT_KEY=Fernet.generate_key().decode(),GIGHOUND_DISTRIBUTED_PACING='0')
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from fastapi import HTTPException
from app.database import Base
from app.models import User, Job, PlatformAccount, ProposalQueueItem, GenerationWork, WorkbenchRecord, StealthTask
from app.routers.proposals import submit_proposal, reconcile_submission, draft_follow_up
from app.schemas import SubmissionReconcileIn
from app.routers.analytics import analytics_funnel
from app.adapters.vault import CredentialVault

def fixture():
    engine=create_engine('sqlite://')
    Base.metadata.create_all(engine)
    Session=sessionmaker(bind=engine,expire_on_commit=False)
    db=Session()
    user=User(email='proof@example.test',password_hash='unused')
    db.add(user);db.flush()
    account=PlatformAccount(user_id=user.id,platform='freelancer',principal='default',label='Test',mode='api',enabled=True,settings={'bidder_id':123})
    db.add(account);db.flush()
    job=Job(user_id=user.id,platform='freelancer',external_id='1234',title='Test',currency='USD',job_type='fixed')
    db.add(job);db.flush()
    p=ProposalQueueItem(user_id=user.id,job_id=job.id,platform='freelancer',status='approved',proposal_text='Reviewed',bid_amount=125,reviewed_by=f'user:{user.id}')
    db.add(p);db.commit()
    return engine,Session,db,user,job,p

class EmptyAdapter:
    def __init__(self,*a,**kw): pass
    async def place_bid(self,**kw): return {}
    async def close(self): pass
engine,S,db,u,j,p=fixture()
with patch('app.adapters.freelancer.FreelancerAdapter',EmptyAdapter):
    result=asyncio.run(submit_proposal(p.id,db,u))
assert result.status=='submitted' and result.submission_result['response']=={}
print('E01 CONFIRMED: empty Freelancer result marks proposal submitted without a receipt')
db.close();engine.dispose()

class SimulatedProcessDeath(BaseException): pass
class CrashAdapter(EmptyAdapter):
    async def place_bid(self,**kw): raise SimulatedProcessDeath()
engine,S,db,u,j,p=fixture()
with patch('app.adapters.freelancer.FreelancerAdapter',CrashAdapter):
    try: asyncio.run(submit_proposal(p.id,db,u))
    except SimulatedProcessDeath: pass
pid,uid=p.id,u.id
db.close()
with S() as db:
    p=db.get(ProposalQueueItem,pid);u=db.get(User,uid)
    assert p.status=='submitting'
    try: reconcile_submission(pid,SubmissionReconcileIn(submitted=False,evidence='Checked platform and no bid exists'),db,u)
    except HTTPException as exc: assert exc.status_code==409
    else: raise AssertionError('reconciliation unexpectedly allowed')
    print('E02 CONFIRMED: interrupted API send leaves durable submitting row with reconciliation rejected')
engine.dispose()

engine,S,db,u,j,p=fixture()
p.status='queued_for_browser';db.commit()
f=analytics_funnel(db,u)
assert f['funnel']['submitted']==1
print('E03 CONFIRMED: queued_for_browser is counted as a completed submission in funnel')
j.currency='JPY';p.bid_amount=10000;db.commit()
f=analytics_funnel(db,u)
assert f['by_bid_band'][-1]['submitted']==1
print('E04 CONFIRMED: 10,000 JPY classified in nominal 1000+ USD bid band without currency/unit normalization')
# Upwork token and session enrollment overwrite, not compose.
from app.routers.credentials import enroll_credentials
from app.schemas import CredentialsIn
account=PlatformAccount(user_id=u.id,platform='upwork',principal='main',label='Upwork',mode='hybrid',enabled=True)
db.add(account);db.commit()
enroll_credentials(account.id,CredentialsIn(secrets={'access_token':'synthetic-token'}),db,u)
enroll_credentials(account.id,CredentialsIn(secrets={'storage_state_json':'{"cookies":[],"origins":[]}'}),db,u)
creds=CredentialVault(db,u.id).load('upwork','main')
assert 'access_token' not in creds
print('E05 CONFIRMED: enrolling browser state after API token removes the API token')
# A date-expired job still passes generation checks if not archived.
from app.orchestrator import generation_gates_pass
j2=Job(user_id=u.id,platform='guru',external_id='expired',title='Expired',apply_deadline=datetime.now(timezone.utc)-timedelta(days=1))
db.add(j2);db.commit()
assert generation_gates_pass(db,j2)
print('E06 CONFIRMED: expired jobs are eligible for generation until archival')
# Alert-enabled rediscovery resurrects manually archived jobs.
from app.models import AlertSettings
from app.schemas import IngestJobsIn
from app.ingest import run_ingest
j2.status='archived';db.add(AlertSettings(user_id=u.id,realtime_enabled=True,min_score_alert=0));db.commit()
with patch('app.ingest.alerts.broadcast',return_value=None) as broadcast:
    asyncio.run(run_ingest(IngestJobsIn(jobs=[{'platform':'guru','external_id':'expired','title':'Expired'}]),db,u))
db.refresh(j2)
assert j2.status=='notified'
print('E07 CONFIRMED: rediscovery with realtime alerts unarchives an archived job')
db.close();engine.dispose()

# Actual overlapping follow-up route executions, synthetic generation only.
with tempfile.TemporaryDirectory(prefix='gighound-proof-') as tmp:
    engine=create_engine('sqlite:///'+tmp+'/proof.db')
    Base.metadata.create_all(engine);S=sessionmaker(bind=engine,expire_on_commit=False)
    with S() as db:
        u=User(email='follow@example.test',password_hash='unused');db.add(u);db.flush()
        j=Job(user_id=u.id,platform='guru',external_id='follow',title='Follow');db.add(j);db.flush()
        p=ProposalQueueItem(user_id=u.id,job_id=j.id,platform='guru',status='submitted',proposal_text='Original');db.add(p);db.commit();uid,pid=u.id,p.id
    async def race():
        ready=asyncio.Event();count=0
        async def fake_generate(*args):
            nonlocal count
            count+=1
            if count==2: ready.set()
            await ready.wait()
            return {'humanized_text':'Follow up','draft_text':'Follow up','typing_plan':[]}
        async def one():
            with S() as db:
                return await draft_follow_up(pid,db,db.get(User,uid))
        with patch('app.proposal_gen.generate_follow_up',fake_generate):
            await asyncio.wait_for(asyncio.gather(one(),one()),3)
    asyncio.run(race())
    with S() as db:
        assert db.query(ProposalQueueItem).filter_by(request_type='follow_up').count()==2
    print('E08 CONFIRMED: concurrent follow-up requests create two active drafts for one parent')
    engine.dispose()

# Readiness probes one historical table, not Alembic head.
from app.main import readiness
engine=create_engine('sqlite://')
with engine.begin() as conn: conn.execute(text('CREATE TABLE generation_work(job_id INTEGER)'))
S=sessionmaker(bind=engine)
with patch('app.database.SessionLocal',S),patch('app.cache.cache._client',return_value=SimpleNamespace(ping=lambda:True)):
    response=readiness()
assert response.status_code==200
print('E09 CONFIRMED: readiness is 200 with almost all application schema absent')
engine.dispose()
# A reviewed browser task does not recheck archive/deadline at final authorization.
from app.routers.gigs import authorize_stealth_action, _flag_session_expired
from app import circuit_breaker as cb
engine,S,db,u,j,p=fixture()
a=PlatformAccount(user_id=u.id,platform='upwork',principal='main',label='Live',mode='hybrid',enabled=True,settings={'on_behalf_of':'member'})
db.add(a);db.flush()
j2=Job(user_id=u.id,platform='upwork',external_id='browser-expiry',title='Reviewed job',url='https://www.upwork.com/jobs/browser-expiry',currency='USD')
db.add(j2);db.flush()
p2=ProposalQueueItem(user_id=u.id,job_id=j2.id,platform='upwork',status='approved',proposal_text='Reviewed text',bid_amount=125)
db.add(p2);db.commit()
p2.status='queued_for_browser'
t=StealthTask(user_id=u.id,platform='upwork',task_type='submit_upwork_proposal',status='claimed',claimed_by='proof',claimed_at=datetime.now(timezone.utc),claim_token='synthetic-claim',payload={'proposal_queue_item_id':p2.id,'proposal_text':p2.proposal_text})
db.add(t);db.commit()
j2.status='archived';j2.apply_deadline=datetime.now(timezone.utc)-timedelta(days=1);db.commit()
with patch.object(cb,'get_state',return_value={'state':'closed'}):
    result=authorize_stealth_action(t.id,{'worker_id':'proof','claim_token':'synthetic-claim'},db,'worker')
assert result['authorized']
print('E10 CONFIRMED: final browser authorization allows archived jobs past their deadline')
# Enrollment/session warnings target oldest account, not task-bound identity.
a.enabled=False
a2=PlatformAccount(user_id=u.id,platform='upwork',principal='second',label='Second',mode='hybrid',enabled=True)
db.add(a2);db.commit()
t.payload={**t.payload,'account_id':a2.id};db.commit()
_flag_session_expired(db,t);db.commit()
assert a.settings.get('needs_reenrollment') and not a2.settings.get('needs_reenrollment')
print('E11 CONFIRMED: session-expired result flags oldest account instead of the task-bound account')
# Reapproval double counts one proposal's template usage.
from app.routers.proposals import approve_proposal, return_to_review
from app.schemas import ProposalReviewAction
from app.models import Template
p.status='pending_review';db.commit()
approve_proposal(p.id,ProposalReviewAction(expected_revision=p.revision,reviewer='Human'),db,u)
tpl=db.get(Template,p.template_id);uses=tpl.uses
return_to_review(p.id,db,u)
approve_proposal(p.id,ProposalReviewAction(expected_revision=p.revision,reviewer='Human'),db,u)
db.refresh(tpl)
assert tpl.uses==uses+1
return_to_review(p.id,db,u)
approve_proposal(p.id,ProposalReviewAction(expected_revision=p.revision,reviewer='Human'),db,u)
db.refresh(tpl)
assert tpl.uses==2
print('E12 CONFIRMED: return-to-review and reapproval inflate template uses for the same proposal')
db.close();engine.dispose()
