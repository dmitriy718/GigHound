#!/usr/bin/env python3
"""Back up the local Compose database and prove recovery in a disposable container.

Uses the source PostgreSQL image's tools, encrypts the dump with its vault key,
and never starts application workers against the restored database. No secrets
are printed or passed as command-line arguments. Run with backend/.venv Python.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import tempfile
import time
from uuid import uuid4

from cryptography.fernet import Fernet
import psycopg2
import backup_restore as archive

ROOT = Path(__file__).resolve().parents[1]
STATE = Path.home() / '.local/state/gighound'


def command(*args, **kwargs):
    return subprocess.check_output(args, cwd=ROOT, stderr=subprocess.PIPE, **kwargs).decode().strip()


def service_id(name):
    result = command('docker', 'compose', 'ps', '-q', name).splitlines()
    if len(result) != 1:
        raise RuntimeError(f'Expected one running local {name} service')
    return result[0]


def inspect(container):
    return json.loads(command('docker', 'inspect', container))[0]


def run():
    os.umask(0o077)
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory = STATE / 'backups'
    directory.mkdir(exist_ok=True, mode=0o700)
    phrase_file = STATE / 'backup-recovery.key'
    if not phrase_file.exists():
        archive.private_write(phrase_file, secrets.token_urlsafe(48).encode())
    if phrase_file.stat().st_mode & 0o077:
        raise ValueError('Recovery key must be accessible only to its owner')
    os.environ['GIGHOUND_BACKUP_PASSPHRASE'] = phrase_file.read_text().strip()
    db_id, app_id = service_id('db'), service_id('backend')
    source = inspect(db_id)
    app_env = dict(entry.split('=', 1) for entry in inspect(app_id)['Config']['Env'] if '=' in entry)
    source_env = dict(entry.split('=', 1) for entry in source['Config']['Env'] if '=' in entry)
    vault_key = app_env.get('GIGHOUND_VAULT_KEY') or app_env.get('GIGHUNTER_VAULT_KEY')
    if not vault_key:
        raise ValueError('Configure a persistent vault key before backing up this installation')
    os.environ['GIGHOUND_VAULT_KEY'] = vault_key
    os.environ['DATABASE_URL'] = app_env['DATABASE_URL']
    name = 'gighound-recovery-' + uuid4().hex[:12]
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    path = directory / f'gighound-{stamp}-{uuid4().hex[:6]}.enc'
    archive.backup(path, dump_command=['docker', 'exec', db_id, 'pg_dump',
        '-U', source_env['POSTGRES_USER'], '-d', source_env['POSTGRES_DB'],
        '--format=custom', '--no-owner', '--no-acl'])
    # Separate target with fresh storage, no source volumes, and an ephemeral
    # loopback-only port. The target contains no application process or broker.
    created = False
    try:
        environment = {**os.environ, 'POSTGRES_PASSWORD': secrets.token_urlsafe(32)}
        created = True  # cleanup also covers interruption during container creation
        command('docker', 'run', '-d', '--name', name,
                '--label', 'gighound.recovery=disposable',
                '-e', 'POSTGRES_PASSWORD', '-e', 'POSTGRES_DB=recovery',
                '-p', '127.0.0.1::5432', source['Image'], env=environment)
        for _ in range(60):
            ready = subprocess.run(['docker', 'exec', name, 'pg_isready', '-h', '127.0.0.1', '-U', 'postgres', '-d', 'recovery'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if ready.returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError('Disposable recovery database did not become ready')
        port = command('docker', 'port', name, '5432/tcp').rsplit(':', 1)[1]
        os.environ['DATABASE_URL'] = f"postgresql://postgres:{environment['POSTGRES_PASSWORD']}@127.0.0.1:{port}/recovery"
        with tempfile.TemporaryDirectory(dir=STATE) as temp:
            key_path = Path(temp) / 'vault.key'
            archive.restore(path, 'recovery', key_path, restore_command=[
                'docker', 'exec', '-i', name, 'pg_restore', '-U', 'postgres',
                '--single-transaction', '--exit-on-error', '--no-owner', '--no-acl', '--dbname', 'recovery'])
            try:
                archive.restore(path, 'recovery', Path(temp) / 'must-not-exist.key', restore_command=['false'])
            except ValueError as exc:
                if str(exc) != 'restore requires an empty target database':
                    raise
            else:
                raise RuntimeError('Nonempty restore target was not rejected')
            cipher = Fernet(key_path.read_bytes())
            with psycopg2.connect(os.environ['DATABASE_URL']) as connection:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT version_num FROM alembic_version')
                    revision = cursor.fetchone()[0]
                    cursor.execute('SELECT count(*) FROM users')
                    users = cursor.fetchone()[0]
                    cursor.execute('SELECT count(*) FROM jobs')
                    jobs = cursor.fetchone()[0]
                    cursor.execute('SELECT blob FROM adapter_credentials')
                    credentials = 0
                    for (blob,) in cursor:
                        decoded = json.loads(cipher.decrypt(blob.encode()))
                        if not isinstance(decoded, dict):
                            raise ValueError('Restored credential is not a JSON object')
                        credentials += 1
        report = {'backup': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                  'verified_at': datetime.now(timezone.utc).isoformat(), 'schema_revision': revision,
                  'users_restored': users, 'jobs_restored': jobs, 'credentials_decrypted': credentials,
                  'recovery_key_file': str(phrase_file), 'database_restore_verified': True, 'nonempty_target_refused': True}
    finally:
        if created:
            subprocess.run(['docker', 'rm', '-f', '-v', name], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)
    archive.private_write(path.with_suffix('.json'), json.dumps(report, indent=2).encode())
    print(json.dumps(report))


if __name__ == '__main__':
    argparse.ArgumentParser(description=__doc__).parse_args()
    def interrupted(*_):
        raise InterruptedError('Backup interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    try:
        run()
    except Exception as exc:
        # Database/process exceptions can contain sensitive connection details.
        print(json.dumps({'backup_verified': False, 'error_type': type(exc).__name__,
                          'message': 'Backup/recovery failed; inspect local configuration and service availability.'}))
        raise SystemExit(1)
