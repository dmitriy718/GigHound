import os, asyncio
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from cryptography.fernet import Fernet
os.environ.update(DATABASE_URL='sqlite://', REDIS_URL='redis://127.0.0.1:1/15', GIGHOUND_SECRET_KEY='audit-only-synthetic-key', GIGHOUND_WORKER_TOKEN='audit-only-worker', GIGHOUND_VAULT_KEY=Fernet.generate_key().decode(), GIGHOUND_DISTRIBUTED_PACING='0')
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.database import Base
from app.models import User, PlatformAccount, AdapterCredential, Job, GenerationWork
from app.adapters.vault import CredentialVault
from app.routers.orchestration import delete_account
from app.adapters.accounts import default_principal
from app.outcome_sync import _client_reply
from app import circuit_breaker as cb
from app.work_queue import reset_generation, claim_generation
engine=create_engine('sqlite://')
Base.metadata.create_all(engine)
with Session(engine) as db:
    user=User(email='audit@example.invalid',password_hash='synthetic')
    db.add(user); db.commit()
    account=PlatformAccount(user_id=user.id,platform='freelancer',principal='default',label='Audit',mode='api',enabled=True)
    db.add(account); db.commit()
    CredentialVault(db,user.id).store('freelancer','default',{'access_token':'synthetic-only'})
    delete_account(account.id,db,user)
    retained=CredentialVault(db,user.id).load('freelancer',default_principal(db,user.id,'freelancer','default'))
    assert retained is not None
    print('P1 CONFIRMED: deleting platform account retains usable vault credentials; legacy selection resolves them')
    job=Job(user_id=user.id,platform='freelancer',external_id='audit-1',title='Audit',url='https://www.freelancer.com/projects/audit')
    db.add(job);db.commit()
    intent=GenerationWork(job_id=job.id,user_id=user.id,state='running',attempts=3,lease_token='expired',lease_until=datetime.now(timezone.utc)-timedelta(hours=1))
    db.add(intent);db.commit()
    assert reset_generation(db,job) is False
    assert claim_generation(db,job) is None
    print('P2 CONFIRMED: third-attempt crash leaves expired running intent neither claimable nor manually resettable')
    item=SimpleNamespace(id=1,submitted_at=datetime.now(timezone.utc)-timedelta(hours=1))
    hit=_client_reply({'project_id':'audit-1','last_message':{'time':datetime.now(timezone.utc).timestamp(),'message':'Unknown sender'}},item,123,job)
    assert hit is not None
    print('P3 CONFIRMED: message with no sender is classified as client reply')
    from app.routers.adapters import freelancer_quota
    from app.adapters.base import AdapterAuthError
    db.add_all([PlatformAccount(user_id=user.id,platform='freelancer',principal=p,label=p,mode='api',enabled=True) for p in ['a','b']]);db.commit()
    try: freelancer_quota(db,user)
    except AdapterAuthError: print('P4 CONFIRMED: quota endpoint raises unhandled AdapterAuthError for two active accounts')
    else: raise AssertionError('expected failure')
with patch.object(cb.cache,'_r',None):
    cb.open_circuit('upwork','synthetic automatic stop',123)
    assert cb.get_state('upwork',123)['state']=='open'
    cb._local.clear()
    assert cb.get_state('upwork',123)['state']=='closed'
    print('P5 CONFIRMED: automatic circuit opens then becomes closed in a fresh process with unavailable Redis')
from app.adapters.freelancer import FreelancerAdapter
class QuotaAdapter(FreelancerAdapter):
    def __init__(self):
        self.used=0;self.calls=0;self.monthly_bid_quota=1
        self.state=SimpleNamespace(set=lambda *args:None)
        self.ready=asyncio.Event()
    def _quota(self): return {'used':self.used}
    def _consume_daily_action(self): pass
    async def _api(self,*args,**kwargs):
        self.calls+=1
        if self.calls==2:self.ready.set()
        await self.ready.wait()
        return {'id':self.calls}
async def race():
    a=QuotaAdapter()
    await asyncio.wait_for(asyncio.gather(a.place_bid(1,1,10,1,'reviewed'),a.place_bid(2,1,10,1,'reviewed')),2)
    assert a.calls==2
    print('P6 CONFIRMED: two concurrent bids reach external API with monthly quota=1; no pre-send reservation')
asyncio.run(race())
from app.workbench import brief
from app.models import WorkbenchRecord
with Session(engine) as db:
    user=db.query(User).first(); job=db.query(Job).first()
    db.add(WorkbenchRecord(user_id=user.id,kind='feedback',reference=f'feedback:{job.id}',data={'kind':'feedback','job_id':job.id,'decision':'skip','reason':'explicit skip'}));db.commit()
    assert not brief(2,15,db,user)['items']
    # Other older job identities are enough to demonstrate the retrieval boundary.
    db.add_all([WorkbenchRecord(user_id=user.id,kind='feedback',reference=f'feedback:other-{i}',data={'kind':'feedback','job_id':1000+i,'decision':'pursue','reason':'other feedback'}) for i in range(1000)])
    db.commit()
    assert brief(2,15,db,user)['items'][0]['job_id']==job.id
    print('P9 CONFIRMED: after 1000 newer feedback records, explicit skip is ignored by daily brief')
