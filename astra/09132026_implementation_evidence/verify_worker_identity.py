"""Owned container protocol proof, using the repository's actual worker Compose service.

No browser is launched, no platform accessed, no user stack changed. Host networking
is used ONLY for this trusted one-shot protocol test; this is not browser isolation proof.
"""
from pathlib import Path
from contextlib import ExitStack
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[2]
SUFFIX = secrets.token_hex(5)
PROJECT = 'gighound-worker-proof-' + SUFFIX
IMAGE = PROJECT + ':verification'
REDIS = PROJECT + '-redis'
EVIDENCE = Path(__file__).resolve().parent


def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def stop(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)
    except ProcessLookupError:
        pass
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


with tempfile.TemporaryDirectory(prefix='worker-identity-', dir=EVIDENCE) as temporary, ExitStack() as cleanup:
    temp = Path(temporary)
    # Keep root shell secrets out of Compose interpolation, and never print expanded config.
    compose_env = {key: os.environ[key] for key in ('PATH', 'DOCKER_HOST', 'DOCKER_CONTEXT', 'XDG_RUNTIME_DIR') if key in os.environ}
    secret = secrets.token_hex(32)
    worker_id = 'synthetic-stable-worker'
    run('docker', 'build', '-t', IMAGE, str(ROOT / 'worker'), stdout=sys.stdout, stderr=sys.stderr)
    cleanup.callback(lambda: subprocess.run(['docker', 'image', 'rm', IMAGE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    run('docker', 'run', '-d', '--name', REDIS, '--label', 'gighound.audit=09132026', '-p', '127.0.0.1::6379', 'valkey/valkey:8-alpine', stdout=subprocess.DEVNULL)
    cleanup.callback(lambda: subprocess.run(['docker', 'rm', '-f', REDIS], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    redis_port = subprocess.check_output(['docker', 'port', REDIS, '6379/tcp'], text=True).strip().rsplit(':', 1)[1]
    for _ in range(100):
        if subprocess.run(['docker', 'exec', REDIS, 'valkey-cli', 'ping'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            break
        time.sleep(.1)
    else:
        raise RuntimeError('owned Redis did not become ready')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
    api_url = f'http://127.0.0.1:{port}'
    env = {**os.environ, 'DATABASE_URL': f'sqlite:///{temp}/api.db', 'REDIS_URL': f'redis://127.0.0.1:{redis_port}/0',
           'GIGHOUND_SECRET_KEY': secrets.token_hex(32), 'GIGHOUND_VAULT_KEY': Fernet.generate_key().decode(),
           'GIGHOUND_DEV_NOAUTH': '0', 'GIGHOUND_WORKER_TOKEN': '',
           'GIGHOUND_WORKER_CREDENTIALS': json.dumps({worker_id: secret}), 'GIGHOUND_ALLOW_REGISTRATION': 'false',
           'LLM_PROVIDER': 'openai', 'LLM_API_KEY': '', 'LLM_BASE_URL': 'http://127.0.0.1:1/v1'}
    run(sys.executable, '-m', 'alembic', 'upgrade', 'head', cwd=ROOT / 'backend', env=env, stdout=subprocess.DEVNULL)
    sys.path.insert(0, str(ROOT / 'backend'))
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.models import User, PlatformAccount, StealthTask
    engine = create_engine(env['DATABASE_URL'])
    cleanup.callback(engine.dispose)
    with Session(engine) as db:
        user = User(email='worker-proof@example.test', password_hash='unused')
        db.add(user); db.flush()
        account = PlatformAccount(user_id=user.id, platform='upwork', label='Synthetic account', mode='hybrid')
        db.add(account); db.flush()
        task = StealthTask(user_id=user.id, platform='upwork', task_type='scrape_proposal_status', payload={'account_id':account.id,'proposals':[]})
        db.add(task); db.commit(); task_id = task.id
    api_log = cleanup.enter_context(open(EVIDENCE / 'worker-identity-api.log', 'w'))
    process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', str(port)], cwd=ROOT / 'backend', env=env, stdout=api_log, stderr=subprocess.STDOUT, start_new_session=True)
    cleanup.callback(stop, process)
    for _ in range(200):
        if process.poll() is not None:
            raise RuntimeError('owned API failed to start; inspect worker-identity-api.log')
        try:
            with urlopen(api_url + '/api/health', timeout=1) as response:
                if response.status == 200: break
        except Exception:
            time.sleep(.1)
    else:
        raise RuntimeError('owned API startup timeout')
    (temp / 'empty.env').write_text('')
    (temp / 'override.json').write_text(json.dumps({'services': {'backend': {'image': 'valkey/valkey:8-alpine'}, 'worker': {'image': IMAGE, 'network_mode': 'host'}}}))
    compose_env.update({'GIGHOUND_API_URL':api_url, 'GIGHOUND_WORKER_TOKEN':secret, 'WORKER_ID':worker_id, 'WORKER_PLATFORMS':'upwork'})
    compose = ['docker','compose','--env-file',str(temp/'empty.env'),'-p',PROJECT,'-f',str(ROOT/'docker-compose.worker.yml'),'-f',str(temp/'override.json')]
    cleanup.callback(lambda: subprocess.run([*compose,'down','--volumes','--remove-orphans'], env=compose_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    code = '''
import os
from pathlib import Path
import httpx
from worker.config import Config
from worker.client import WorkerClient, BackendError, ClaimConflictError
cfg=Config();cfg.validate()
assert cfg.worker_id == 'synthetic-stable-worker'
assert os.getuid() != 0
assert all(not os.environ.get(name) for name in ('DATABASE_URL','LLM_API_KEY','GIGHOUND_VAULT_KEY','GIGHOUND_SECRET_KEY','GIGHOUND_WORKER_CREDENTIALS'))
assert not list(Path('/app/worker').glob('.env*'))
client=WorkerClient(cfg.api_url,cfg.worker_token,cfg.worker_id,max_retries=1)
try:
    assert client.heartbeat(cfg.platforms)['recorded'] is True
    tasks=client.poll_tasks('upwork');assert len(tasks)==1
    task=client.claim_task(tasks[0].id);assert task.claim_token
    assert client.authorize_task(task.id)['authorized'] is True
    assert client.complete_task(task.id,True,{'synthetic_protocol_check':True})['status']=='done'
    try: client.complete_task(task.id,True,{})
    except ClaimConflictError: pass
    else: raise AssertionError('completed claim accepted twice')
    for token,identity in [(cfg.worker_token,'forged-worker'),('wrong-synthetic-token',cfg.worker_id)]:
        bad=WorkerClient(cfg.api_url,token,identity,max_retries=1)
        try:
            try: bad.poll_tasks('upwork')
            except BackendError: pass
            else: raise AssertionError('unregistered credentials accepted')
        finally: bad.close()
    r=httpx.post(cfg.api_url+'/api/gigs/worker-heartbeat',headers={'Authorization':'Bearer '+cfg.worker_token,'X-Worker-ID':cfg.worker_id},json={'worker_id':'forged-body','platforms':['upwork']})
    assert r.status_code==403
finally:client.close()
print('PASS: Compose stable identity, non-root runtime, secret exclusions, heartbeat, poll, fenced claim, authorization, completion, replay refusal and forged credentials refusal')
'''
    run(*compose, 'run', '--rm', '--no-deps', '-T', 'worker', 'python', '-c', code, env=compose_env)
    with Session(engine) as db:
        task=db.get(StealthTask,task_id)
        assert task.status=='done' and task.claimed_by==worker_id
    print('PASS: database confirms the owned task completed under the registered stable worker ID')
