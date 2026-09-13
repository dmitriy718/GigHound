"""Run tests against disposable services; never connect to the deployed stack."""
import os, subprocess, time, uuid
from pathlib import Path
root = Path(__file__).resolve().parents[2]
def run(*args, **kw):
    return subprocess.check_output(args, text=True, **kw).strip()
suffix=uuid.uuid4().hex[:10]
pg='gighound-verify-pg-'+suffix
redis='gighound-verify-redis-'+suffix
owned=[]
try:
    for name,image,extra,port in [(pg,'postgres:16.15-alpine',['-e','POSTGRES_PASSWORD=synthetic-audit-only','-e','POSTGRES_DB=gighound_test'],'5432'),(redis,'valkey/valkey:8-alpine',[],'6379')]:
        run('docker','run','-d','--name',name,'--label','gighound.audit=09132026','-p','127.0.0.1::'+port,*extra,image)
        owned.append(name)
    for _ in range(60):
        if subprocess.run(['docker','exec',pg,'pg_isready','-h','127.0.0.1','-U','postgres','-d','gighound_test'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0: break
        time.sleep(.5)
    else: raise RuntimeError('disposable PostgreSQL not ready')
    pgport=run('docker','port',pg,'5432/tcp').rsplit(':',1)[1]
    rport=run('docker','port',redis,'6379/tcp').rsplit(':',1)[1]
    env={**os.environ,'PYTHONPATH':str(root/'backend'),'DATABASE_URL':'sqlite://',
         'GIGHOUND_TEST_POSTGRES_URL':f'postgresql+psycopg2://postgres:synthetic-audit-only@127.0.0.1:{pgport}/gighound_test',
         'GIGHOUND_TEST_REDIS_URL':f'redis://127.0.0.1:{rport}/15'}
    migration_env={**env, 'DATABASE_URL':env['GIGHOUND_TEST_POSTGRES_URL'],
                   'GIGHOUND_SECRET_KEY':'synthetic-migration-verification-only'}
    def alembic(*args):
        subprocess.run([str(root/'backend/.venv/bin/python'),'-m','alembic',*args],cwd=root/'backend',env=migration_env,check=True)
    alembic('upgrade','df5c687b9401')
    # This owned database has no deployment or customer records.
    seed_code = """
import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from app.models import User
engine=create_engine(os.environ['DATABASE_URL'])
with Session(engine) as db:
    db.add(User(email='migration@example.test',password_hash='synthetic'))
    db.commit()
"""
    subprocess.run([str(root/'backend/.venv/bin/python'),'-c',seed_code],env=migration_env,check=True)
    alembic('upgrade','head')
    alembic('check')
    verify_code = """
import os
from sqlalchemy import create_engine, text
engine=create_engine(os.environ['DATABASE_URL'])
with engine.connect() as db:
    rows=db.execute(text('SELECT state,manual_stop,revision FROM automation_circuits')).all()
    assert len(rows)==7 and all(tuple(r)==('open',True,1) for r in rows),rows
    db.execute(text('SELECT platform_account_id FROM proposal_queue LIMIT 1'))
"""
    subprocess.run([str(root/'backend/.venv/bin/python'),'-c',verify_code],env=migration_env,check=True)
    alembic('downgrade','base')
    alembic('upgrade','head')
    alembic('check')
    print('PASS: PostgreSQL migration pause, schema checks and complete migration round trip',flush=True)
    result=subprocess.run([str(root/'backend/.venv/bin/python'),'-m','pytest','backend/tests','-q'],cwd=root,env=env)
    raise SystemExit(result.returncode)
finally:
    for name in owned:
        subprocess.run(['docker','rm','-f',name],stdout=subprocess.DEVNULL)
