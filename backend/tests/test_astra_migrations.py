"""Destructive migration cases use isolated in-memory fixtures only."""
import importlib.util
from pathlib import Path
from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import create_engine, text


def migration(prefix, connection):
    path = next((Path(__file__).parents[1]/'alembic/versions').glob(prefix+'*.py'))
    spec = importlib.util.spec_from_file_location('tested_migration', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


def test_duplicate_live_migration_preserves_evidence():
    engine = create_engine('sqlite://')
    with engine.begin() as db:
        db.execute(text('CREATE TABLE proposal_queue (id INTEGER PRIMARY KEY, job_id INTEGER, status TEXT, request_type TEXT)'))
        db.execute(text("INSERT INTO proposal_queue VALUES (1,42,'submitted','job'),(2,42,'submitted_unverified','job')"))
        with pytest.raises(RuntimeError, match='Duplicate live proposals.*42'):
            migration('e4a91b6c2d08', db).upgrade()
        assert db.execute(text('SELECT count(*) FROM proposal_queue')).scalar() == 2
        db.execute(text("UPDATE proposal_queue SET status='failed' WHERE id=2"))
        migration('e4a91b6c2d08', db).upgrade()
        assert db.execute(text('SELECT count(*) FROM proposal_queue')).scalar() == 2


def test_unknown_metrics_cannot_downgrade_to_fabricated_zeros():
    engine = create_engine('sqlite://')
    with engine.begin() as db:
        db.execute(text('CREATE TABLE gig_metrics (id INTEGER PRIMARY KEY, impressions INTEGER, clicks INTEGER, orders INTEGER, revenue FLOAT)'))
        db.execute(text('INSERT INTO gig_metrics VALUES (1,NULL,0,NULL,NULL)'))
        with pytest.raises(RuntimeError, match='fabricated zeros'):
            migration('ce4b576a8390', db).downgrade()
        assert db.execute(text('SELECT impressions FROM gig_metrics')).scalar() is None


def test_circuit_migration_pauses_existing_tenants_without_inventing_cache_state():
    engine = create_engine('sqlite://')
    with engine.begin() as db:
        db.execute(text('CREATE TABLE users (id INTEGER PRIMARY KEY)'))
        db.execute(text('INSERT INTO users VALUES (42)'))
        module = migration('e06d713ca502', db)
        module.upgrade()
        rows = db.execute(text('SELECT state,manual_stop,user_id FROM automation_circuits')).all()
        assert len(rows) == 7
        assert all(row == ('open', 1, 42) for row in rows)
        module.downgrade()


def test_account_identity_migration_assigns_distinct_epochs():
    engine = create_engine('sqlite://')
    with engine.begin() as db:
        db.execute(text('CREATE TABLE platform_accounts (id INTEGER PRIMARY KEY)'))
        db.execute(text('INSERT INTO platform_accounts VALUES (1),(2)'))
        module=migration('a28f935ec724',db)
        module.upgrade()
        epochs=db.execute(text('SELECT identity_epoch FROM platform_accounts ORDER BY id')).scalars().all()
        from uuid import UUID
        assert len(epochs)==len(set(epochs))==2
        assert all(str(UUID(epoch))==epoch for epoch in epochs)
        module.downgrade()
        assert db.execute(text('SELECT count(*) FROM platform_accounts')).scalar()==2
