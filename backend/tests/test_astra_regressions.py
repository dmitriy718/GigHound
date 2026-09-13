import asyncio
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.database import Base
from app.models import User, Job, ProposalQueueItem, SearchProfile, SearchFilter, StealthTask
from app.cache import cache

@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(cache,'_r',None)
    monkeypatch.setattr(cache,'get_json',lambda *a,**k:None)
    monkeypatch.setattr(cache,'set_json',lambda *a,**k:None)
    from app import circuit_breaker
    circuit_breaker._local.clear(); circuit_breaker._local_trials.clear()
    engine=create_engine('sqlite://'); Base.metadata.create_all(engine)
    s=sessionmaker(bind=engine,expire_on_commit=False)()
    s.add(User(id=1,email='audit@example.test',password_hash='unused'))
    s.commit()
    yield s
    s.close(); engine.dispose()

def proposal(db, **kw):
    j=Job(user_id=1,platform=kw.pop('platform','upwork'),external_id='proof',title='React project',description='React development',quality_score=90)
    db.add(j); db.flush()
    p=ProposalQueueItem(user_id=1,job_id=j.id,platform=j.platform,proposal_text='old draft',humanized_text='old draft',**kw)
    db.add(p); db.commit(); return p

def test_approved_edit_invalidates_generated_text(db):
    from app.routers.proposals import approve_proposal, revert_version
    from app.schemas import ProposalReviewAction
    p = proposal(db)
    approve_proposal(p.id, ProposalReviewAction(expected_revision=1, reviewer='human', proposal_text='approved edit', save_as_template=False), db, db.get(User, 1))
    assert p.proposal_text == 'approved edit'
    assert p.humanized_text == '' and p.typing_plan == []
    revert_version(p.id, {'version_index': 0}, db, db.get(User, 1))
    assert p.proposal_text == 'old draft'
    assert p.humanized_text == '' and p.status == 'pending_review'


def test_circuit_controls_are_tenant_scoped(db):
    from app.routers.gigs import set_circuit
    from app.circuit_breaker import check
    set_circuit('upwork', {'state': 'open'}, db.get(User, 1), db)
    assert check('upwork', 1)[0] is False
    assert check('upwork', 2)[0] is True
    assert check('upwork')[0] is True


@pytest.mark.parametrize('failure', ['response', 'transport'])
def test_writes_are_never_automatically_replayed(failure):
    import httpx
    from app.adapters.ratelimit import request_with_retry
    calls = []
    def handle(request):
        calls.append(request)
        if failure == 'transport':
            raise httpx.ReadTimeout('response lost', request=request)
        return httpx.Response(503)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(httpx.HTTPError):
                await request_with_retry(client, 'POST', 'https://example.test/bid')
    asyncio.run(run())
    assert len(calls) == 1


def test_empty_success_does_not_confirm_a_submission(db):
    from app.routers.gigs import _apply_submission_outcome
    p = proposal(db, platform='fiverr', status='queued_for_browser')
    task = StealthTask(user_id=1, platform='fiverr', task_type='submit_fiverr_offer', payload={'proposal_queue_item_id': p.id}, result={})
    _apply_submission_outcome(db, task, True)
    assert p.status == 'submitted_unverified'


def test_unsupported_platform_filter_does_not_search_everywhere(db):
    from app.discovery import platforms_for_profile
    f = SearchFilter(user_id=1, name='Fiverr only', platforms=['fiverr'])
    db.add(f); db.flush()
    profile = SearchProfile(user_id=1, name='Fiverr only', filter_id=f.id)
    assert platforms_for_profile(db, profile) == []


@pytest.mark.parametrize('route', ['freelancer_bid', 'upwork_submit_proposal'])
def test_legacy_routes_reject_wrong_platform(db, route):
    from fastapi import HTTPException
    from app.routers import adapters
    from app.routers.adapters import QueueItemAction
    p = proposal(db, platform='guru', status='approved')
    with pytest.raises(HTTPException) as exc:
        asyncio.run(getattr(adapters, route)(QueueItemAction(proposal_queue_item_id=p.id), db, db.get(User, 1)))
    assert exc.value.status_code == 409


def test_all_profiles_opted_out_disables_generation(db):
    from app.orchestrator import generation_gates_pass, build_pipeline_context
    p = proposal(db); job = db.get(Job, p.job_id); db.delete(p)
    db.add(SearchProfile(user_id=1, name='manual only', auto_queue_proposals=False)); db.commit()
    assert generation_gates_pass(db, job, build_pipeline_context(db, 1)) is False
    assert generation_gates_pass(db, job) is False


@pytest.mark.parametrize('submitted', [True, False])
def test_reconciliation_requires_new_review_or_confirmed_submission(db, submitted):
    from fastapi import HTTPException
    from app.routers.proposals import reconcile_submission
    from app.schemas import SubmissionReconcileIn
    p = proposal(db, status='submitted_unverified', reviewed_by='old reviewer')
    body = SubmissionReconcileIn(submitted=submitted, evidence='Checked my platform proposal list')
    reconcile_submission(p.id, body, db, db.get(User, 1))
    assert p.status == ('submitted' if submitted else 'pending_review')
    if not submitted:
        assert p.reviewed_by is None
    with pytest.raises(HTTPException) as exc:
        reconcile_submission(p.id, body, db, db.get(User, 1))
    assert exc.value.status_code == 409


def test_password_limit_counts_utf8_bytes():
    from pydantic import ValidationError
    from app.schemas import RegisterIn, PasswordChangeIn
    with pytest.raises(ValidationError):
        RegisterIn(email='a@example.test', password='é' * 37)
    with pytest.raises(ValidationError):
        PasswordChangeIn(current_password='old', new_password='é' * 37)
    assert RegisterIn(email='a@example.test', password='é' * 36).password == 'é' * 36


def test_client_requirements_do_not_become_freelancer_strengths(db):
    from app.proposal_gen import skill_portfolio_match
    p = proposal(db); job = db.get(Job, p.job_id)
    job.skills = ['Rust', 'Kubernetes']
    match = skill_portfolio_match(db, job, {}, items=[])
    assert match['strengths'] == []
    assert match['gaps'] == ['kubernetes', 'rust']


def test_approval_and_template_wait_for_one_commit(db):
    from sqlalchemy import event
    from app.models import AuditLog
    from app.routers.proposals import approve_proposal
    from app.schemas import ProposalReviewAction
    p = proposal(db)
    commits = []
    def before_commit(session):
        # Flush is allowed, but the audit event must exist before durability.
        assert any(isinstance(row, AuditLog) and row.action_type == 'proposal_approved'
                   for row in session.new)
        commits.append(True)
    event.listen(db, 'before_commit', before_commit)
    try:
        approve_proposal(p.id, ProposalReviewAction(expected_revision=1, reviewer='human', save_as_template=True), db, db.get(User, 1))
    finally:
        event.remove(db, 'before_commit', before_commit)
    assert commits == [True]


def test_default_test_fixture_never_connects_to_or_flushes_redis(monkeypatch):
    from unittest.mock import Mock
    import conftest
    import redis
    monkeypatch.delenv('GIGHOUND_TEST_REDIS_URL', raising=False)
    connect = Mock()
    monkeypatch.setattr(redis.Redis, 'from_url', connect)
    fixture = conftest._flush_test_redis.__wrapped__()
    next(fixture)
    with pytest.raises(StopIteration):
        next(fixture)
    connect.assert_not_called()


def test_reconciliation_handles_a_newer_active_draft(db):
    from fastapi import HTTPException
    from app.routers.proposals import reconcile_submission
    from app.schemas import SubmissionReconcileIn
    old = proposal(db, status='failed')
    newer = ProposalQueueItem(user_id=1, job_id=old.job_id, platform=old.platform,
                              proposal_text='New draft', status='pending_review')
    db.add(newer); db.commit()
    with pytest.raises(HTTPException) as exc:
        reconcile_submission(old.id, SubmissionReconcileIn(submitted=False, evidence='Checked the platform history'), db, db.get(User, 1))
    assert exc.value.status_code == 409
    assert db.get(ProposalQueueItem, old.id).status == 'failed'
    assert db.get(ProposalQueueItem, newer.id).status == 'pending_review'


def test_durable_revocation_survives_redis_loss(db):
    from datetime import datetime, timedelta, timezone
    from app.auth import create_access_token, decode_token, get_user_from_token
    from app.models import AuthTransaction
    user = db.get(User, 1)
    token = create_access_token(user)
    assert get_user_from_token(db, token).id == 1
    user.session_version += 1
    db.commit()
    assert get_user_from_token(db, token) is None
    token = create_access_token(user)
    db.add(AuthTransaction(id='revoked:' + decode_token(token)['jti'], user_id=1, kind='revoked',
                           payload={}, expires_at=datetime.now(timezone.utc)+timedelta(hours=12)))
    db.commit()
    assert get_user_from_token(db, token) is None


def test_generation_lease_fences_stale_results(db):
    from datetime import datetime, timedelta, timezone
    from app.models import GenerationWork
    from app.work_queue import claim_generation, finish_generation
    from app.orchestrator import _generation_write_allowed
    p = proposal(db, status='generation_failed')
    job = db.get(Job, p.job_id)
    token = claim_generation(db, job)
    assert token and claim_generation(db, job) is None
    work = db.get(GenerationWork, job.id)
    work.lease_until = datetime.now(timezone.utc)-timedelta(seconds=1)
    db.commit()
    newer = claim_generation(db, job)
    assert newer and newer != token
    db.info['generation_lease'] = (job.id, token)
    assert not _generation_write_allowed(db, job, p)
    finish_generation(db, job.id, token, True)
    db.refresh(work)
    assert work.lease_token == newer and work.state == 'running'
    db.info['generation_lease'] = (job.id, newer)
    assert _generation_write_allowed(db, job, p)


def test_outcome_repeat_and_correction_rebuilds_totals(db):
    from app.models import Template
    from app.templates import record_outcome
    p = proposal(db, status='submitted', bid_amount=200)
    tpl = Template(user_id=1, platform='upwork', title='t', text='t', source_proposal_id=p.id)
    db.add(tpl); db.commit()
    p.template_id = tpl.id; db.commit()
    assert record_outcome(db, p, 'hired')
    assert not record_outcome(db, p, 'hired')
    assert tpl.wins == 1 and tpl.losses == 0
    assert record_outcome(db, p, 'rejected')
    assert tpl.wins == 0 and tpl.losses == 1 and tpl.win_rate == 0
    p.status = 'pending_review'; db.commit()
    with pytest.raises(ValueError):
        record_outcome(db, p, 'hired')


def test_submission_timestamp_is_confirmation_not_approval(db):
    from datetime import datetime, timedelta, timezone
    p = proposal(db, status='approved', reviewed_at=datetime.now(timezone.utc)-timedelta(days=10))
    assert p.submitted_at is None
    p.status = 'submitted'; db.commit()
    assert p.submitted_at > p.reviewed_at
    stamp = p.submitted_at
    p.rejection_notes = 'later annotation'; db.commit()
    assert p.submitted_at == stamp


def test_workbench_tenancy_versions_and_attribution(db):
    from fastapi import HTTPException
    from app.workbench import RecordIn, RecordEdit, create, edit, owned, roi
    from app.models import WorkbenchRecord
    user = db.get(User, 1)
    other = User(email='other@example.test', password_hash='unused'); db.add(other); db.commit()
    data = {'kind':'evidence','title':'Delivery result','claim':'Reduced build time by 20%',
            'source':'case-study.pdf page 3','verified_by_user':True}
    row = create(RecordIn(data=data), db, user)
    with pytest.raises(HTTPException) as exc:
        owned(db, WorkbenchRecord, row['id'], other)
    assert exc.value.status_code == 404
    newer = edit(row['id'], RecordEdit(data={**data,'title':'Corrected title'}, expected_version=1), db, user)
    assert newer['version'] == 2
    with pytest.raises(HTTPException) as exc:
        edit(row['id'], RecordEdit(data=data, expected_version=1), db, user)
    assert exc.value.status_code == 409
    p = proposal(db, status='pending_review')
    receipt = {'kind':'revenue','title':'Payment','proposal_id':p.id,'reference':'invoice-123',
               'currency':'EUR','amount':'100.10','cost':'20.05','effort_hours':'3.50',
               'received_at':'2026-09-05T12:00:00Z'}
    with pytest.raises(HTTPException):
        create(RecordIn(data=receipt), db, user)
    p.status = 'submitted'; db.commit()
    create(RecordIn(data=receipt), db, user)
    with pytest.raises(HTTPException) as exc:
        create(RecordIn(data=receipt), db, user)
    assert exc.value.status_code == 409
    assert roi(db, user)['currencies']['EUR']['net'] == '80.05'
    assert roi(db, other)['currencies'] == {}


def test_scope_decimal_currency_capacity_and_validation(db):
    from app.workbench import Scope, scope
    from pydantic import ValidationError
    body = dict(title='API', deliverables='Two endpoints', assumptions='Client provides API access',
                currency='EUR', hours_low=10, hours_high=20, cost_per_hour='40.50',
                expenses='100', margin_percent=25, available_hours=15)
    result = scope(Scope(**body), db.get(User, 1))
    assert result['price_low'] == '673.33' and result['capacity_exceeded']
    assert result['currency'] == 'EUR'
    with pytest.raises(ValidationError):
        Scope(**{**body, 'margin_percent':100})
    with pytest.raises(ValidationError):
        Scope(**{**body, 'cost_per_hour':'NaN'})


def test_browser_destination_and_storage_boundaries():
    from app.browser_security import validate_platform_url, validate_storage_state
    assert validate_platform_url('upwork','https://www.upwork.com/jobs/123')
    for url in ['http://upwork.com', 'https://upwork.com.evil.test/', 'https://127.0.0.1/',
                'https://user:pass@upwork.com', 'https://upwork.com:8443/']:
        with pytest.raises(ValueError):
            validate_platform_url('upwork', url)
    with pytest.raises(ValueError):
        validate_storage_state('upwork',{'origins':[{'origin':'https://fiverr.com'}]})


def test_team_roles_acceptance_stale_review_and_revocation(db):
    from app.teamwork import (TeamIn,MemberIn,DraftIn,EditDraft,ReviewIn,create_team,invite,
                              accept,access,create_draft,review,edit_draft,remove_member)
    from fastapi import HTTPException
    owner=db.get(User,1)
    contributor=User(email='contributor@example.test',password_hash='unused')
    reviewer=User(email='reviewer@example.test',password_hash='unused')
    db.add_all([contributor,reviewer]);db.commit()
    team=create_team(TeamIn(name='Agency'),db,owner)
    invite(team['id'],MemberIn(email=contributor.email,role='contributor'),db,owner)
    invite(team['id'],MemberIn(email=reviewer.email,role='reviewer'),db,owner)
    with pytest.raises(HTTPException):access(db,team['id'],contributor)
    accept(team['id'],db,contributor);accept(team['id'],db,reviewer)
    with pytest.raises(HTTPException) as exc:
        invite(team['id'],MemberIn(email='nobody@example.test',role='reviewer'),db,contributor)
    assert exc.value.status_code==403
    body=dict(title='Reviewed draft',text='Evidence grounded text',destination='platform job 42',assignee_id=contributor.id)
    d=create_draft(team['id'],DraftIn(**body),db,contributor)
    with pytest.raises(HTTPException) as exc:
        review(team['id'],d['id'],ReviewIn(expected_version=1,decision='approved'),db,contributor)
    assert exc.value.status_code==403
    approved=review(team['id'],d['id'],ReviewIn(expected_version=1,decision='approved'),db,reviewer)
    assert approved['version']==2 and approved['reviewed_by']==reviewer.id
    with pytest.raises(HTTPException):
        edit_draft(team['id'],d['id'],EditDraft(**body,expected_version=1),db,contributor)
    edited=edit_draft(team['id'],d['id'],EditDraft(**{**body,'text':'Changed scope'},expected_version=2),db,contributor)
    assert edited['status']=='pending_review' and edited['reviewed_by'] is None
    remove_member(team['id'],contributor.id,db,owner)
    with pytest.raises(HTTPException):access(db,team['id'],contributor)


def test_profile_negative_does_not_block_other_profile(db,monkeypatch):
    from app.models import KeywordGroup,Keyword
    from app.ingest import run_ingest
    from app.schemas import IngestJobsIn
    monkeypatch.setattr('app.tasks.generate_proposal_task.delay',lambda *a:None)
    group=KeywordGroup(user_id=1,name='exclude frontend');db.add(group);db.flush()
    db.add(Keyword(group_id=group.id,term='React',kind='negative',weight=1))
    db.add_all([SearchProfile(user_id=1,name='backend only',boolean_query='',keyword_group_id=group.id,auto_queue_proposals=True),
                SearchProfile(user_id=1,name='frontend',boolean_query='React',auto_queue_proposals=True)])
    db.commit()
    result=asyncio.run(run_ingest(IngestJobsIn(jobs=[dict(platform='upwork',external_id='scoped-neg',title='React project',description='Build React dashboard',url='https://www.upwork.com/jobs/1')]),db,db.get(User,1)))
    job=db.query(Job).filter_by(external_id='scoped-neg').one()
    assert job.status!='archived'
    from app.models import GenerationWork
    assert db.get(GenerationWork,job.id) is not None


def test_snapshot_rejects_changed_destination_and_stale_review(db):
    from app.approval import require_snapshot
    from app.routers.proposals import approve_proposal
    from app.schemas import ProposalReviewAction
    from fastapi import HTTPException
    p = proposal(db)
    version = p.revision
    p.proposal_text = 'newer draft'; db.commit()
    with pytest.raises(HTTPException) as exc:
        approve_proposal(p.id, ProposalReviewAction(reviewer='spoofed display label',expected_revision=version), db, db.get(User,1))
    assert exc.value.status_code == 409
    approve_proposal(p.id, ProposalReviewAction(reviewer='display label',expected_revision=p.revision), db, db.get(User,1))
    assert p.reviewed_by == 'user:1'
    assert require_snapshot(db,p)['text'] == 'newer draft'
    job = db.get(Job,p.job_id)
    job.url = 'https://www.upwork.com/different-job'; db.commit()
    with pytest.raises(HTTPException):require_snapshot(db,p)


def test_task_account_cannot_silently_switch(db):
    from app.models import PlatformAccount
    from app.routers.gigs import _require_browser_account
    from fastapi import HTTPException
    old = PlatformAccount(user_id=1,platform='upwork',label='old',mode='stealth')
    db.add(old);db.commit()
    task = StealthTask(user_id=1,platform='upwork',task_type='submit_upwork_proposal',payload={})
    db.add(task);db.commit()
    assert task.payload['account_id'] == old.id
    old.enabled = False
    replacement = PlatformAccount(user_id=1,platform='upwork',label='replacement',principal='replacement',mode='stealth')
    db.add(replacement);db.commit()
    with pytest.raises(HTTPException):_require_browser_account(db,task)


def test_registered_worker_identity_and_rotation(monkeypatch):
    import json
    from fastapi.security import HTTPAuthorizationCredentials
    from fastapi import HTTPException
    from app.auth import get_worker,is_worker_token
    secret='worker-a-test-key-with-at-least-32-characters'
    credentials=HTTPAuthorizationCredentials(scheme='Bearer',credentials=secret)
    monkeypatch.setenv('GIGHOUND_WORKER_CREDENTIALS',json.dumps({'worker-a':secret}))
    assert get_worker(credentials,'worker-a')=='worker-a'
    with pytest.raises(HTTPException):get_worker(credentials,'worker-b')
    monkeypatch.setenv('GIGHOUND_WORKER_CREDENTIALS',json.dumps({'worker-a':secret+'-rotated'}))
    assert not is_worker_token(credentials)


def test_send_budget_is_durable_and_idempotent(db,monkeypatch):
    from app.send_budget import reserve_send
    from app.models import AuthTransaction
    from fastapi import HTTPException
    monkeypatch.setenv('GIGHOUND_DAILY_SUBMIT_CAP_UPWORK','1')
    a=StealthTask(user_id=1,platform='upwork',task_type='submit_upwork_proposal',payload={})
    b=StealthTask(user_id=1,platform='upwork',task_type='submit_upwork_proposal',payload={})
    db.add_all([a,b]);db.commit()
    reserve_send(db,a);db.commit()
    reserve_send(db,a);db.commit()
    assert db.query(AuthTransaction).filter_by(kind='send_attempt').count()==1
    with pytest.raises(HTTPException) as exc:reserve_send(db,b)
    assert exc.value.status_code==409


def test_json_bounds_before_route_work():
    from app.request_bounds import RequestBounds
    called=[]
    async def app(scope,receive,send):called.append(True)
    async def exercise(body):
        messages=[]
        async def receive():return {'type':'http.request','body':body}
        async def send(message):messages.append(message)
        await RequestBounds(app,max_bytes=100)({'type':'http','method':'POST','headers':[(b'content-type',b'application/json')]},receive,send)
        return messages[0]['status']
    assert asyncio.run(exercise(b'x'*101))==413
    assert asyncio.run(exercise(b'{"amount":NaN}'))==422
    assert not called


def test_manual_stop_survives_empty_broker_and_process_cache(db):
    from app.routers.gigs import set_circuit, circuit_state, _require_browser_account
    from app.models import PlatformAccount
    from app import circuit_breaker
    user = db.get(User, 1)
    db.add(PlatformAccount(user_id=1, platform='upwork', label='Test account', principal='default', enabled=True, mode='stealth'))
    task = StealthTask(user_id=1, platform='upwork', task_type='scrape_proposal_status', payload={})
    db.add(task); db.commit()
    set_circuit('upwork', {'state':'open', 'reason':'operator stop'}, user, db)
    circuit_breaker._local.clear()
    assert circuit_state('upwork', user, db)['state'] == 'open'
    with pytest.raises(Exception) as stopped:
        _require_browser_account(db, task)
    assert stopped.value.status_code == 409
    set_circuit('upwork', {'state':'closed'}, user, db)
    assert _require_browser_account(db, task).user_id == 1


def test_half_open_trial_is_owned_and_completion_resolves_it(db):
    from app.send_budget import reserve_circuit_trials, finish_circuit_trials
    from app import circuit_breaker
    from fastapi import HTTPException
    tasks = [StealthTask(user_id=1, platform='upwork', task_type='scrape_proposal_status', payload={}) for _ in range(2)]
    db.add_all(tasks); db.commit()
    circuit_breaker.transition('upwork', 'half_open', user_id=1)
    reserve_circuit_trials(db, tasks[0]); db.commit()
    with pytest.raises(HTTPException, match='another task'):
        reserve_circuit_trials(db, tasks[1])
    finish_circuit_trials(db, tasks[1], True)
    assert circuit_breaker.get_state('upwork', 1)['state'] == 'half_open'
    finish_circuit_trials(db, tasks[0], True); db.commit()
    assert circuit_breaker.get_state('upwork', 1)['state'] == 'closed'


def test_approval_requires_revision_and_credential_rotation_invalidates(db, monkeypatch):
    from pydantic import ValidationError
    from cryptography.fernet import Fernet
    from app.models import PlatformAccount
    from app.schemas import ProposalReviewAction
    from app.routers.proposals import approve_proposal
    from app.adapters.vault import CredentialVault
    from app.approval import require_snapshot
    from fastapi import HTTPException
    with pytest.raises(ValidationError):
        ProposalReviewAction(reviewer='human')
    monkeypatch.setenv('GIGHOUND_VAULT_KEY', Fernet.generate_key().decode())
    db.add(PlatformAccount(user_id=1, platform='upwork', label='Test account', principal='default', enabled=True, mode='stealth')); db.commit()
    vault = CredentialVault(db, 1)
    vault.store('upwork', 'default', {'storage_state_json':'{"cookies":[]}'})
    item = proposal(db)
    approve_proposal(item.id, ProposalReviewAction(expected_revision=item.revision, reviewer='human'), db, db.get(User, 1))
    require_snapshot(db, item)
    vault.store('upwork', 'default', {'storage_state_json':'{"cookies":[],"origins":[]}'})
    with pytest.raises(HTTPException):
        require_snapshot(db, item)


def test_cross_currency_requires_recent_attributed_rates(monkeypatch):
    import json
    from datetime import datetime, timedelta, timezone
    from app.fx import conversion_factor
    monkeypatch.delenv('GIGHOUND_FX_RATES_JSON', raising=False)
    assert conversion_factor('INR', 'INR') == 1
    assert conversion_factor('USD', 'INR') is None
    record = {'as_of':datetime.now(timezone.utc).isoformat(), 'source':'synthetic fixture', 'usd_per_unit':{'INR':0.012}}
    monkeypatch.setenv('GIGHOUND_FX_RATES_JSON', json.dumps(record))
    assert conversion_factor('USD', 'INR') == pytest.approx(83.3333333333)
    record['as_of'] = (datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
    monkeypatch.setenv('GIGHOUND_FX_RATES_JSON', json.dumps(record))
    assert conversion_factor('USD', 'INR') is None


def test_offline_interview_does_not_invent_experience_or_dates(db):
    from app.proposal_gen import _interview_prep_offline
    item = proposal(db)
    result = _interview_prep_offline(db.get(Job,item.job_id),item,{'required_skills':['React']},[])
    content = str(result)
    assert '[add a verified portfolio example]' in content
    assert 'first week' not in content and 'my daily work' not in content and 'recent comparable project' not in content
    assert 'No delivery date is committed' in content


def test_portfolio_pages_have_stable_boundaries_and_tenant_scope(db):
    from app.models import PortfolioItem
    from app.routers.profiles import list_portfolio
    db.add(User(id=2,email='other-pages@example.test',password_hash='unused'))
    db.add_all([PortfolioItem(user_id=1,title=f'Proof {n}') for n in range(201)])
    db.add(PortfolioItem(user_id=2,title='Private other tenant'))
    db.commit()
    pages=[list_portfolio(db,db.get(User,1),limit=100,offset=n) for n in (0,100,200)]
    assert [len(page) for page in pages] == [100,100,1]
    ids=[p.id for page in pages for p in page]
    assert len(set(ids)) == 201 and ids == sorted(ids)
    assert all(p.user_id == 1 for page in pages for p in page)


def test_implicit_adapter_reads_use_enrolled_principal_and_reject_ambiguity(db):
    from app.models import PlatformAccount
    from app.adapters.freelancer import FreelancerAdapter
    from app.adapters.base import AdapterAuthError
    db.add(PlatformAccount(user_id=1,platform='freelancer',label='Read account',principal='secondary',mode='api',enabled=True)); db.commit()
    adapter = FreelancerAdapter(db,1)
    assert adapter.credential_principal == 'secondary'
    asyncio.run(adapter.close())
    db.add(PlatformAccount(user_id=1,platform='freelancer',label='Ambiguous account',principal='third',mode='api',enabled=True)); db.commit()
    with pytest.raises(AdapterAuthError,match='multiple active accounts'):
        FreelancerAdapter(db,1)


def test_platform_account_deletion_revokes_only_its_vault_credentials(db):
    from app.models import PlatformAccount, AdapterCredential
    from app.adapters.vault import CredentialVault
    from app.routers.orchestration import delete_account
    user = db.get(User, 1)
    account = PlatformAccount(user_id=1, platform='freelancer', principal='default', label='Remove')
    db.add(account); db.commit()
    vault = CredentialVault(db, 1)
    vault.store('freelancer', 'default', {'access_token': 'synthetic'})
    vault.store('upwork', 'agency_manager', {'access_token': 'keep-synthetic'})
    delete_account(account.id, db, user)
    assert vault.load('freelancer', 'default') is None
    assert vault.load('upwork', 'agency_manager') is not None
    replacement = PlatformAccount(user_id=1, platform='freelancer', principal='default', label='Re-enrolled')
    db.add(replacement); db.commit()
    assert vault.load('freelancer', replacement.principal) is None


@pytest.mark.parametrize('field,value', [('bid_amount', 999), ('job_url', 'https://www.upwork.com/jobs/wrong'), ('job_external_id', 'wrong'), ('on_behalf_of', 'wrong'), ('agency_id', 'wrong'), ('connects_required', 999)])
def test_upwork_authorization_rejects_material_payload_drift(db, field, value):
    from datetime import datetime, timezone
    from fastapi import HTTPException
    from app.models import PlatformAccount
    from app.routers.gigs import authorize_stealth_action
    db.add(PlatformAccount(user_id=1, platform='upwork', label='agency', mode='hybrid', settings={'agency_id': 'agency-1', 'on_behalf_of': 'member-1'}))
    db.commit()
    p = proposal(db, status='approved', bid_amount=123)
    approved = dict(p.approved_snapshot)
    p.status = 'queued_for_browser'; db.commit()
    payload = {'proposal_queue_item_id': p.id, 'proposal_text': approved['text'],
               'job_url': approved['destination'], 'job_external_id': approved['job_external_id'],
               'bid_amount': approved['bid'], 'on_behalf_of': approved['agency_member'],
               'agency_id': 'agency-1', 'connects_required': approved['connects_required']}
    task = StealthTask(user_id=1, platform='upwork', task_type='submit_upwork_proposal',
                       payload=payload, status='claimed', claimed_by='audit-worker',
                       claimed_at=datetime.now(timezone.utc), claim_token='synthetic-claim')
    db.add(task); db.commit()
    body = {'worker_id': 'audit-worker', 'claim_token': 'synthetic-claim'}
    assert authorize_stealth_action(task.id, body, db, 'worker')['authorized']
    task.payload = {**task.payload, field: value}; db.commit()
    with pytest.raises(HTTPException) as error:
        authorize_stealth_action(task.id, body, db, 'worker')
    assert error.value.status_code == 409


def test_expired_final_generation_attempt_can_be_retried_and_old_worker_is_fenced(db):
    from datetime import datetime, timedelta, timezone
    from app.models import GenerationWork
    from app.work_queue import expire_exhausted, reset_generation, claim_generation, finish_generation
    p = proposal(db, status='generation_failed')
    job = db.get(Job, p.job_id)
    work = GenerationWork(job_id=job.id, user_id=1, state='running', attempts=3,
                          lease_token='old-worker', lease_until=datetime.now(timezone.utc)-timedelta(minutes=1))
    db.add(work); db.commit()
    expire_exhausted(db); db.refresh(work)
    assert work.state == 'failed'
    assert reset_generation(db, job)
    token = claim_generation(db, job)
    assert token and token != 'old-worker'
    assert not reset_generation(db, job)
    finish_generation(db, job.id, 'old-worker', True)
    db.refresh(work)
    assert work.state == 'running' and work.lease_token == token


def test_skipped_job_stays_excluded_after_thousand_newer_feedback_records(db):
    from app.workbench import brief
    from app.models import WorkbenchRecord
    p = proposal(db)
    db.add(WorkbenchRecord(user_id=1, kind='feedback', reference=f'feedback:{p.job_id}',
                           data={'job_id': p.job_id, 'decision': 'skip', 'reason': 'not suitable'}))
    db.commit()
    db.add_all([WorkbenchRecord(user_id=1, kind='feedback', reference=f'feedback:synthetic-{i}',
                                data={'job_id': 1000+i, 'decision': 'pursue'}) for i in range(1000)])
    db.commit()
    assert brief(2, 15, db, db.get(User, 1))['items'] == []


def test_retry_generation_without_proposal_retains_intent_on_broker_failure(db, monkeypatch):
    from app.workbench import retry_generation_work
    from app.models import GenerationWork
    from app import tasks
    job = Job(user_id=1, platform='freelancer', external_id='no-proposal', title='Unfinished generation')
    db.add(job); db.flush()
    row = GenerationWork(job_id=job.id, user_id=1, state='failed', attempts=3)
    db.add(row); db.commit()
    def unavailable(*args): raise RuntimeError('synthetic broker outage')
    monkeypatch.setattr(tasks.generate_proposal_task, 'delay', unavailable)
    assert retry_generation_work(job.id, db, db.get(User, 1))['delivery'] == 'waiting_for_broker'
    db.refresh(row)
    assert row.state == 'pending' and row.attempts == 0


@pytest.mark.parametrize('email', ['invalid', 'a@', 'a@bad_domain.test', 'a@-bad.test', 'a..b@example.test', 'a@example.test\nBcc:secret@example.test'])
def test_new_registration_rejects_invalid_mailboxes(email):
    from pydantic import ValidationError
    from app.schemas import RegisterIn
    with pytest.raises(ValidationError):
        RegisterIn(email=email, password='synthetic-password')


def test_team_invitation_uses_canonical_registration_email(db):
    from app.teamwork import create_team, invite, TeamIn, MemberIn
    from app.schemas import RegisterIn
    canonical = RegisterIn(email='  Teammate@Example.Test ', password='synthetic-password').email
    teammate = User(email=canonical, password_hash='synthetic')
    db.add(teammate); db.commit()
    owner = db.get(User, 1)
    team = create_team(TeamIn(name='Synthetic team'), db, owner)
    result = invite(team['id'], MemberIn(email=' TEAMMATE@example.test ', role='reviewer'), db, owner)
    assert result['user_id'] == teammate.id


def test_stale_refresh_cannot_restore_deleted_or_rotated_credentials(db):
    from app.adapters.vault import CredentialVault
    from app.adapters.base import AdapterAuthError
    from app.models import PlatformAccount
    from app.routers.orchestration import delete_account
    account = PlatformAccount(user_id=1, platform='freelancer', principal='default', label='Account')
    db.add(account); db.commit()
    vault = CredentialVault(db, 1, account_id=account.id)
    vault.store('freelancer', 'default', {'access_token': 'old-synthetic'})
    stale = CredentialVault(db, 1)
    stale.load('freelancer', 'default')
    CredentialVault(db, 1, account_id=account.id).store('freelancer', 'default', {'access_token': 'rotated-synthetic'})
    with pytest.raises(AdapterAuthError, match='changed'):
        stale.store('freelancer', 'default', {'access_token': 'late-refresh'})
    db.rollback()
    stale.load('freelancer', 'default')
    delete_account(account.id, db, db.get(User, 1))
    with pytest.raises(AdapterAuthError, match='changed'):
        stale.store('freelancer', 'default', {'access_token': 'late-after-delete'})
    db.rollback()
    assert CredentialVault(db, 1).load('freelancer', 'default') is None
    with pytest.raises(AdapterAuthError, match='removed'):
        vault.store('freelancer', 'default', {'access_token': 'late-enrollment'})
    db.rollback()


def test_oauth_exchange_cannot_overwrite_enrollment_during_provider_wait(db):
    from app.adapters.vault import CredentialVault
    from app.adapters.freelancer import FreelancerAdapter
    from app.adapters.base import AdapterAuthError
    import httpx
    old = CredentialVault(db, 1)
    old.store('freelancer', 'default', {'access_token': 'old-synthetic'})
    async def run():
        def response(request):
            CredentialVault(db, 1).store('freelancer', 'default', {'access_token': 'new-enrollment'})
            return httpx.Response(200, json={'access_token': 'late-oauth', 'expires_in': 3600})
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            adapter = FreelancerAdapter(db, 1, client=client, principal='default')
            with pytest.raises(AdapterAuthError, match='changed'):
                await adapter.exchange_code('synthetic-id','synthetic-secret','synthetic-code','https://example.test/callback')
    asyncio.run(run())
    db.rollback()
    assert CredentialVault(db, 1).load('freelancer', 'default')['access_token'] == 'new-enrollment'


def test_automatic_circuit_survives_cache_loss_and_rollbacks(db, monkeypatch):
    from app import circuit_breaker as cb
    cb.open_circuit('upwork', 'automatic failure', 1, db=db); db.commit()
    cb._local.clear(); cb._local_trials.clear()
    monkeypatch.setattr(cb.cache, '_r', None)
    assert cb.get_state('upwork', 1, db=db)['state'] == 'open'
    cb.close_circuit('upwork', 'uncommitted resolution', 1, db=db)
    db.rollback()
    assert cb.get_state('upwork', 1, db=db)['state'] == 'open'
    cb.close_circuit('upwork', 'committed resolution', 1, db=db); db.commit()
    assert cb.check('upwork', 1, db=db)[0]


def test_circuit_database_outage_never_grants_admission(monkeypatch):
    from app import circuit_breaker as cb
    from sqlalchemy.exc import OperationalError
    def unavailable(): raise OperationalError('synthetic outage', None, RuntimeError('offline'))
    monkeypatch.setattr(cb, 'SessionLocal', unavailable)
    assert cb.get_state('upwork', 1)['state'] == 'open'
    assert cb.check('upwork', 1)[0] is False


def test_old_trial_cannot_resolve_a_new_circuit_revision(db):
    from app import circuit_breaker as cb
    from app.send_budget import reserve_circuit_trials, finish_circuit_trials
    tasks = [StealthTask(user_id=1, platform='upwork', task_type='scrape_proposal_status', payload={}) for _ in range(2)]
    db.add_all(tasks); db.commit()
    cb.transition('upwork','half_open',user_id=1,db=db)
    reserve_circuit_trials(db,tasks[0]); db.commit()
    cb.transition('upwork','open',user_id=1,db=db)
    cb.transition('upwork','half_open',user_id=1,db=db)
    reserve_circuit_trials(db,tasks[1]); db.commit()
    finish_circuit_trials(db,tasks[0],True); db.commit()
    assert cb.get_state('upwork',1,db=db)['state']=='half_open'
    finish_circuit_trials(db,tasks[1],True); db.commit()
    assert cb.get_state('upwork',1,db=db)['state']=='closed'


def test_durable_manual_circuit_blocks_api_platform_gate(db):
    from app import circuit_breaker as cb
    from app.auth import platform_enabled
    cb.transition('freelancer','open','migration pause',1,manual_stop=True,db=db);db.commit()
    assert not platform_enabled(db,1,'freelancer')
    cb.close_circuit('freelancer','reviewed',1,db=db);db.commit()
    assert platform_enabled(db,1,'freelancer')


def test_review_requires_owned_explicit_account_and_keeps_selection(db):
    from app.models import PlatformAccount
    from app.routers.proposals import approve_proposal
    from app.schemas import ProposalReviewAction
    from app.approval import require_snapshot
    from fastapi import HTTPException
    first = PlatformAccount(label="Synthetic", user_id=1, platform='upwork', principal='first', mode='hybrid', settings={'on_behalf_of':'alice'})
    second = PlatformAccount(label="Synthetic", user_id=1, platform='upwork', principal='second', mode='hybrid', settings={'on_behalf_of':'bob'})
    db.add_all([first,second]); db.commit()
    p = proposal(db)
    user = db.get(User,1)
    with pytest.raises(HTTPException) as ambiguous:
        approve_proposal(p.id, ProposalReviewAction(expected_revision=p.revision,reviewer='human',save_as_template=False),db,user)
    assert ambiguous.value.status_code == 409
    db.rollback()
    with pytest.raises(HTTPException):
        approve_proposal(p.id, ProposalReviewAction(expected_revision=p.revision,reviewer='human',platform_account_id=999,save_as_template=False),db,user)
    db.rollback()
    approve_proposal(p.id, ProposalReviewAction(expected_revision=p.revision,reviewer='human',platform_account_id=second.id,save_as_template=False),db,user)
    assert p.platform_account_id == second.id
    assert require_snapshot(db,p)['agency_member'] == 'bob'
    first.settings = {'on_behalf_of':'changed unrelated account'}; db.commit()
    assert require_snapshot(db,p)['account_id'] == second.id
    second.enabled = False; db.commit()
    with pytest.raises(HTTPException): require_snapshot(db,p)


def test_explicit_task_account_is_not_replaced_by_other_active_account(db):
    from app.models import PlatformAccount
    from app.routers.gigs import _require_browser_account
    from fastapi import HTTPException
    account = PlatformAccount(label="Synthetic", user_id=1,platform='upwork',principal='available',mode='hybrid')
    db.add(account); db.commit()
    task = StealthTask(user_id=1,platform='upwork',task_type='submit_upwork_proposal',payload={'account_id':999})
    db.add(task); db.commit()
    assert task.payload['account_id'] == 999
    with pytest.raises(HTTPException): _require_browser_account(db,task)


def test_api_account_resolution_rejects_ambiguity_and_foreign_accounts(db):
    from app.models import PlatformAccount
    from app.adapters.accounts import selected_principal
    from fastapi import HTTPException
    db.add(User(id=2,email='other-account@example.test',password_hash='unused')); db.flush()
    accounts = [PlatformAccount(label="Synthetic", user_id=uid,platform='freelancer',principal=principal,mode='api') for uid,principal in [(1,'one'),(1,'two'),(2,'foreign')]]
    db.add_all(accounts); db.commit()
    with pytest.raises(HTTPException) as ambiguous: selected_principal(db,1,'freelancer',None,'default')
    assert ambiguous.value.status_code == 409
    assert selected_principal(db,1,'freelancer',accounts[1].id,'default') == 'two'
    with pytest.raises(HTTPException): selected_principal(db,1,'freelancer',accounts[2].id,'default')


def test_agency_rosters_are_isolated_and_removals_persist(db):
    from app.adapters.upwork_agency import UpworkAgencyAdapter
    async def run():
        first = UpworkAgencyAdapter(db,1,principal='agency_manager')
        second = UpworkAgencyAdapter(db,1,principal='second')
        try:
            first.add_agency_member('alice')
            second.add_agency_member('bob')
            assert [m['username'] for m in first.list_agency_members()] == ['alice']
            assert [m['username'] for m in second.list_agency_members()] == ['bob']
            second.remove_agency_member('bob')
            db.expire_all()
            assert second.list_agency_members() == []
            assert [m['username'] for m in first.list_agency_members()] == ['alice']
        finally:
            await first.close(); await second.close()
    asyncio.run(run())


def test_discovery_uses_each_owned_enabled_account(db, monkeypatch):
    from app.models import PlatformAccount
    from app import discovery
    db.add_all([PlatformAccount(label='Synthetic',user_id=1,platform='freelancer',principal=p,mode='api',enabled=enabled) for p,enabled in [('one',True),('two',True),('off',False)]])
    db.commit()
    calls=[]
    async def search(db,user,platform,terms,principal=None):
        calls.append(principal)
        return [principal]
    monkeypatch.setattr(discovery,'_search_account',search)
    assert asyncio.run(discovery._search_platform(db,db.get(User,1),'freelancer',['python'])) == ['one','two']
    assert calls == ['one','two']


def test_dispatch_uses_selected_principal_and_bidder_not_first_account(db, monkeypatch):
    from app.models import PlatformAccount
    from app.routers.proposals import approve_proposal, submit_proposal
    from app.schemas import ProposalReviewAction
    accounts=[PlatformAccount(label='Synthetic',user_id=1,platform='freelancer',principal=principal,mode='api',settings={'bidder_id':bidder}) for principal,bidder in [('first',111),('second',222)]]
    db.add_all(accounts); db.commit()
    p=proposal(db,platform='freelancer',bid_amount=125,submission_result={'bidder_id':111})
    job=db.get(Job,p.job_id); job.external_id='123'; db.commit()
    user=db.get(User,1)
    approve_proposal(p.id,ProposalReviewAction(expected_revision=p.revision,reviewer='human',platform_account_id=accounts[1].id,save_as_template=False),db,user)
    calls=[]
    class Adapter:
        def __init__(self,db,user_id,*,principal): calls.append(('principal',principal))
        async def place_bid(self,**kwargs): calls.append(('bidder',kwargs['bidder_id'])); return {'id':456}
        async def close(self): pass
    monkeypatch.setattr('app.adapters.freelancer.FreelancerAdapter',Adapter)
    result=asyncio.run(submit_proposal(p.id,db,user))
    assert result.status=='submitted'
    assert calls==[('principal','second'),('bidder',222)]
    assert p.approved_snapshot['account_id']==accounts[1].id


def test_delayed_enrollment_cannot_cross_reused_account_id(db):
    from app.models import PlatformAccount
    from app.adapters.vault import CredentialVault
    from app.adapters.base import AdapterAuthError
    from app.routers.orchestration import delete_account
    account = PlatformAccount(label='Old synthetic',user_id=1,platform='freelancer',principal='default')
    db.add(account); db.commit()
    account_id = account.id
    old_epoch = account.identity_epoch
    vault = CredentialVault(db,1,account_id=account_id)
    delete_account(account_id,db,db.get(User,1))
    replacement = PlatformAccount(id=account_id,label='New synthetic',user_id=1,platform='freelancer',principal='default')
    db.add(replacement); db.commit()
    assert replacement.identity_epoch != old_epoch
    with pytest.raises(AdapterAuthError,match='removed'):
        vault.store('freelancer','default',{'access_token':'late old OAuth'})
    db.rollback()
    assert CredentialVault(db,1).load('freelancer','default') is None


def test_browser_status_tasks_group_only_the_reviewed_account(db):
    from app.models import PlatformAccount
    from app.proposal_status_sync import enqueue_platform_status_scrapes
    accounts=[PlatformAccount(label='Synthetic',user_id=1,platform='upwork',principal=p,mode='hybrid') for p in ['one','two']]
    db.add_all(accounts); db.flush()
    items=[]
    for index,account_id in enumerate([accounts[0].id,accounts[1].id,None]):
        job=Job(user_id=1,platform='upwork',external_id=f'status-{index}',title='Synthetic')
        db.add(job); db.flush()
        item=ProposalQueueItem(user_id=1,job_id=job.id,platform='upwork',platform_account_id=account_id,status='submitted')
        db.add(item); items.append(item)
    db.commit()
    tasks=enqueue_platform_status_scrapes(db,1)
    groups={t.payload['account_id']:[i['proposal_queue_item_id'] for i in t.payload['items']] for t in tasks}
    assert groups=={accounts[0].id:[items[0].id],accounts[1].id:[items[1].id]}
    assert enqueue_platform_status_scrapes(db,1)==[]
    tasks[0].status='done'; db.commit()
    retry=enqueue_platform_status_scrapes(db,1)
    assert len(retry)==1 and retry[0].payload['account_id']==tasks[0].payload['account_id']


def test_fiverr_monitor_fetches_each_account_without_stacking(db, monkeypatch):
    from app.models import PlatformAccount
    from app import tasks
    db.add_all([PlatformAccount(label='Synthetic',user_id=1,platform='fiverr',principal=p,mode='stealth',settings={'username':p}) for p in ['seller-one','seller-two']]); db.commit()
    monkeypatch.setattr(tasks,'SessionLocal',sessionmaker(bind=db.bind,expire_on_commit=False))
    result=tasks.fiverr_buyer_request_tick_core()
    assert len(result['enqueued'])==2
    fetched=[db.get(StealthTask,i) for i in result['enqueued']]
    assert {t.payload['username'] for t in fetched}=={'seller-one','seller-two'}
    assert len({t.payload['account_id'] for t in fetched})==2
    assert tasks.fiverr_buyer_request_tick_core()['enqueued']==[]


def test_fiverr_generated_offer_preserves_source_account(db, monkeypatch):
    from app.models import PlatformAccount
    from app import fiverr_monitor
    account=PlatformAccount(label='Synthetic',user_id=1,platform='fiverr',principal='second',mode='stealth')
    db.add(account); db.commit()
    monkeypatch.setattr(fiverr_monitor,'matching_buyer_requests',lambda db,uid,requests:requests)
    monkeypatch.setattr(fiverr_monitor,'_counter',lambda *args:1)
    monkeypatch.setattr(fiverr_monitor,'offers_remaining_today',lambda *args:9)
    result=fiverr_monitor.process_buyer_requests(db,1,[{'id':'source-proof','title':'Synthetic request','budget':100,'currency':'EUR'}],account_id=account.id)
    assert result['queued']==1
    item=db.query(ProposalQueueItem).one()
    assert item.platform_account_id==account.id
    assert db.get(Job,item.job_id).score_breakdown['source_account_id']==account.id


@pytest.mark.parametrize('change', [
    {'from_user':None},{'from_user':111},{'from_user':333},{'from_user':''},
    {'time':True},{'time':float('nan')},{'time':1e100},{'message':{}},
])
def test_reply_detection_rejects_unknown_sender_or_invalid_message(db,change):
    from datetime import datetime,timedelta,timezone
    from app.outcome_sync import _client_reply
    p=proposal(db,platform='freelancer',status='submitted',submitted_at=datetime.now(timezone.utc)-timedelta(hours=1))
    job=db.get(Job,p.job_id);job.client_info={'client_id':'222'}
    last={'from_user':222,'time':datetime.now(timezone.utc).timestamp()-10,'message':'A valid synthetic message',**change}
    assert _client_reply({'project_id':job.external_id,'last_message':last},p,111,job) is None


def test_reply_pagination_reaches_beyond_250_and_records_freshness(db,monkeypatch):
    from datetime import datetime,timedelta,timezone
    from app.outcome_sync import _sync_account_threads, reply_cursor_key
    from app.adapters.vault import StateStore
    p=proposal(db,platform='freelancer',status='submitted',submitted_at=datetime.now(timezone.utc)-timedelta(hours=1),submission_result={'bidder_id':111})
    job=db.get(Job,p.job_id);job.client_info={'client_id':'222'};db.commit()
    offsets=[];events=[]
    class Adapter:
        async def get_threads(self,limit=50,offset=0):
            offsets.append(offset)
            if offset < 300: return [{'project_id':f'unrelated-{offset+i}'} for i in range(50)]
            return [{'project_id':job.external_id,'last_message':{'from_user':222,'time':datetime.now(timezone.utc).timestamp()-10,'message':'Late-page reply'}}]
    async def broadcast(uid,event): events.append(event)
    monkeypatch.setattr('app.outcome_sync.alerts.broadcast',broadcast)
    user=db.get(User,1)
    assert asyncio.run(_sync_account_threads(db,user,Adapter(),'default',{}))==(0,5)
    state=StateStore(db,1).get('freelancer',reply_cursor_key('default'))
    assert state['offset']==250 and state['last_success_at'] and not state.get('last_complete_scan_at')
    assert asyncio.run(_sync_account_threads(db,user,Adapter(),'default',{}))==(1,2)
    assert offsets==[0,50,100,150,200,250,300]
    assert len(events)==1 and p.client_replied_at is not None
    state=StateStore(db,1).get('freelancer',reply_cursor_key('default'))
    assert state['offset']==0 and state['last_complete_scan_at']
    assert StateStore(db,1).get('freelancer',reply_cursor_key('other')) is None
    # A later replay updates no proposal and emits no duplicate alert.
    StateStore(db,1).set('freelancer',reply_cursor_key('default'),{'offset':300})
    assert asyncio.run(_sync_account_threads(db,user,Adapter(),'default',{}))==(0,1)
    assert len(events)==1


def test_legacy_roster_requires_owned_explicit_assignment_once(db):
    from app.models import PlatformAccount, AdapterState
    from app.routers.adapters import assign_legacy_agency_roster
    from app.adapters.upwork_agency import UpworkAgencyAdapter
    from fastapi import HTTPException
    first=PlatformAccount(label='First',user_id=1,platform='upwork',principal='agency_manager')
    second=PlatformAccount(label='Second',user_id=1,platform='upwork',principal='second')
    db.add_all([first,second,AdapterState(user_id=1,platform='upwork',key='agency_roster',value={'members':[{'username':'legacy-member','status':'invitation_pending'}]})]);db.commit()
    async def check():
        a=UpworkAgencyAdapter(db,1,principal='agency_manager')
        b=UpworkAgencyAdapter(db,1,principal='second')
        try:
            assert a.list_agency_members()==b.list_agency_members()==[]
            with pytest.raises(HTTPException): assign_legacy_agency_roster(999,db,db.get(User,1))
            db.rollback()
            assert assign_legacy_agency_roster(second.id,db,db.get(User,1))['members'][0]['username']=='legacy-member'
            assert a.list_agency_members()==[]
            assert b.list_agency_members()[0]['username']=='legacy-member'
            with pytest.raises(HTTPException): assign_legacy_agency_roster(first.id,db,db.get(User,1))
        finally:
            await a.close();await b.close()
    asyncio.run(check())


def test_freelancer_threads_use_official_messages_endpoint(db):
    import httpx
    from app.adapters.freelancer import FreelancerAdapter
    from app.adapters.vault import CredentialVault
    CredentialVault(db,1).store('freelancer','default',{'access_token':'synthetic'})
    calls=[]
    def response(request):
        calls.append(request)
        return httpx.Response(200,json={'status':'success','result':{'threads':[]}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            adapter=FreelancerAdapter(db,1,client=client,principal='default')
            assert await adapter.get_threads(limit=50,offset=300)==[]
    asyncio.run(run())
    assert calls[0].url.path=='/api/messages/0.1/threads/'
    assert calls[0].url.params['last_message']=='true'
    assert calls[0].url.params['context_details']=='true'
    assert calls[0].url.params['offset']=='300'


def test_official_nested_thread_context_and_sender_are_recognized(db):
    from datetime import datetime,timedelta,timezone
    from app.outcome_sync import _client_reply
    p=proposal(db,platform='freelancer',status='submitted',submitted_at=datetime.now(timezone.utc)-timedelta(hours=1))
    job=db.get(Job,p.job_id);job.client_info={'client_id':'222'}
    thread={'thread':{'id':300,'context':{'type':'project','id':job.external_id}},
            'last_message':{'from_user_id':222,'time':datetime.now(timezone.utc).timestamp()-10,'message':'Synthetic nested-context reply'}}
    assert _client_reply(thread,p,111,job) is not None
    thread['thread']['context']['type']='contest'
    assert _client_reply(thread,p,111,job) is None


def test_documented_thread_messages_and_creation_time_detect_reply(db):
    from datetime import datetime,timedelta,timezone
    from app.outcome_sync import _client_reply
    p=proposal(db,platform='freelancer',status='submitted',submitted_at=datetime.now(timezone.utc)-timedelta(hours=1))
    job=db.get(Job,p.job_id);job.client_info={'client_id':'222'}
    now=datetime.now(timezone.utc).timestamp()
    row={'thread':{'id':300,'context':{'type':'project','id':job.external_id}},'messages':[
        {'from_user':111,'time_created':now-5,'message':'Our later message'},
        {'from_user':222,'time_created':now-10,'message':'Client message still included in this page'}]}
    assert _client_reply(row,p,111,job)==(now-10,'Client message still included in this page')


def test_freelancer_bid_status_uses_documented_collection_and_checks_identity(db):
    import httpx
    from app.adapters.freelancer import FreelancerAdapter
    from app.adapters.vault import CredentialVault
    from app.adapters.base import AdapterAuthError
    CredentialVault(db,1).store('freelancer','default',{'access_token':'synthetic'})
    calls=[]
    def response(request):
        calls.append(request)
        return httpx.Response(200,json={'status':'success','result':{'bids':[{'id':77,'award_status':'awarded'}]}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            adapter=FreelancerAdapter(db,1,client=client,principal='default')
            assert (await adapter.get_bid_status(77))['award_status']=='awarded'
            with pytest.raises(AdapterAuthError): await adapter.get_bid_status(99)
    asyncio.run(run())
    assert calls[0].url.path=='/api/projects/0.1/bids/' and calls[0].url.params['bids[]']=='77'
    assert calls[0].headers['Freelancer-OAuth-V1']=='synthetic'


def test_indeed_connector_is_not_offered_but_manual_ingest_remains_valid(db):
    from app.routers.orchestration import create_account
    from app.schemas import PlatformAccountIn, JobIngest
    from app.platforms import DISCOVERY_PLATFORMS
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        create_account(PlatformAccountIn(platform='indeed',label='Unsupported'),db,db.get(User,1))
    assert exc.value.status_code==422
    assert JobIngest(platform='indeed',external_id='manual',title='Manually imported job').platform=='indeed'
    assert 'indeed' not in DISCOVERY_PLATFORMS


def test_browser_task_cannot_follow_reused_account_id(db):
    from fastapi import HTTPException
    from app.models import PlatformAccount
    from app.routers.gigs import _require_browser_account
    account = PlatformAccount(user_id=1, platform='upwork', principal='original', label='Original', mode='hybrid')
    db.add(account); db.commit()
    account_id = account.id
    task = StealthTask(user_id=1, platform='upwork', task_type='scrape_proposal_status', payload={'account_id':account_id})
    db.add(task); db.commit()
    assert _require_browser_account(db, task).principal == 'original'
    db.delete(account); db.commit()
    replacement = PlatformAccount(id=account_id, user_id=1, platform='upwork', principal='replacement', label='Replacement', mode='hybrid')
    db.add(replacement); db.commit()
    with pytest.raises(HTTPException) as stopped:
        _require_browser_account(db, task)
    assert stopped.value.status_code == 409
