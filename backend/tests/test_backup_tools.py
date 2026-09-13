"""Restore key lifecycle; actual PostgreSQL restoration is verified separately."""
from contextlib import contextmanager
import importlib.util
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location('backup_restore_test', Path(__file__).resolve().parents[2] / 'scripts/backup_restore.py')
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


@pytest.fixture
def empty_target(monkeypatch):
    @contextmanager
    def cursor():
        yield SimpleNamespace(execute=lambda *_: None, fetchone=lambda: (0,))
    monkeypatch.setenv('DATABASE_URL', 'postgresql://synthetic:unused@localhost/recovery')
    monkeypatch.setattr('psycopg2.connect', lambda **_: SimpleNamespace(cursor=cursor, close=lambda: None))
    monkeypatch.setattr(backup, 'unpack', lambda _: {'database.dump': b'dump', 'vault.key': b'synthetic-key'})


def test_restore_stages_key_before_database_restore(empty_target, monkeypatch, tmp_path):
    destination = tmp_path / 'vault.key'
    def restore_command(*args, **kwargs):
        assert destination.read_bytes() == b'synthetic-key'
        assert destination.stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr(backup.subprocess, 'run', restore_command)
    backup.restore(tmp_path / 'backup.enc', 'recovery', destination)
    assert destination.exists()


def test_failed_restore_cleans_key_and_existing_key_is_preserved(empty_target, monkeypatch, tmp_path):
    destination = tmp_path / 'vault.key'
    def fail(*args, **kwargs):
        assert destination.exists()
        raise subprocess.CalledProcessError(1, 'pg_restore')
    monkeypatch.setattr(backup.subprocess, 'run', fail)
    with pytest.raises(subprocess.CalledProcessError):
        backup.restore(tmp_path / 'backup.enc', 'recovery', destination)
    assert not destination.exists()
    destination.write_bytes(b'pre-existing')
    with pytest.raises(ValueError, match='already exists'):
        backup.restore(tmp_path / 'backup.enc', 'recovery', destination)
    assert destination.read_bytes() == b'pre-existing'


def test_unwritable_key_destination_prevents_restore(empty_target, monkeypatch, tmp_path):
    def must_not_run(*args, **kwargs):
        pytest.fail('Database restore started without a writable key destination')
    monkeypatch.setattr(backup.subprocess, 'run', must_not_run)
    with pytest.raises(FileNotFoundError):
        backup.restore(tmp_path / 'backup.enc', 'recovery', tmp_path / 'missing' / 'vault.key')
