"""Shared test configuration.

Sets a JWT secret before any app module is imported (app.config reads env
at import time) so the real auth flow works under TestClient. Schema in
tests is created via Base.metadata.create_all on in-memory SQLite — no
Alembic needed. A fixed vault key is set so the credential vault works
without GIGHOUND_DEV_NOAUTH (which must stay off: auth tests rely on it).
"""
import os

# Adapter unit tests use local pacing; a dedicated Redis concurrency proof exercises distributed admission.
os.environ["GIGHOUND_DISTRIBUTED_PACING"] = "0"

os.environ.setdefault("GIGHOUND_SECRET_KEY", "test-secret-key")
os.environ.setdefault("GIGHOUND_WORKER_TOKEN", "test-worker-token")

# Redis tests are opt-in: GIGHOUND_TEST_REDIS_URL must name a disposable database.
# The fixture FLUSHES that database. By default use a closed port.
# Isolate the suite from any live dev stack: the app and tests share
# localhost:6379 otherwise, and the running celery-beat's pacing locks /
# circuit-breaker keys (discovery:{user}:{platform}, circuit:*) collide with
# test user ids and make results timing-dependent. Hard-set (not setdefault)
# so it also wins over backend/.env via config.load_dotenv.
os.environ["REDIS_URL"] = os.environ.get("GIGHOUND_TEST_REDIS_URL", "redis://127.0.0.1:1/15")

from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("GIGHOUND_VAULT_KEY", Fernet.generate_key().decode())

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _flush_test_redis():
    """Start each test from a clean slate on the isolated db: pacing locks
    and circuit-breaker keys must not leak between tests (discovery tests
    share user id 1 + platforms and collide via discovery:{user}:{platform}
    locks within one run). No-op when Redis is down — the suite is designed
    to degrade gracefully without it."""
    if not os.environ.get("GIGHOUND_TEST_REDIS_URL"):
        yield
        return
    try:
        import redis

        r = redis.Redis.from_url(os.environ["REDIS_URL"], socket_timeout=1)
        r.ping()
        r.flushdb()
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def _isolated_circuit_sessions(monkeypatch, request, tmp_path):
    """Exercise SQL circuits in the same disposable DB as each test, never .env."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app import circuit_breaker
    from app.models import AutomationCircuit
    engine = create_engine(f'sqlite:///{tmp_path}/circuits.sqlite')
    AutomationCircuit.__table__.create(engine)
    fallback = sessionmaker(bind=engine)

    def sessions():
        from app.main import app
        from app.database import get_db
        override = app.dependency_overrides.get(get_db)
        if override:
            # Extract the session bind without borrowing the request transaction.
            generator = override()
            session = next(generator)
            bind = session.get_bind()
            generator.close()
            return sessionmaker(bind=bind)()
        if 'db' in request.fixturenames:
            session = request.getfixturevalue('db')
            if hasattr(session, 'get_bind'):
                return sessionmaker(bind=session.get_bind())()
        return fallback()
    monkeypatch.setattr(circuit_breaker, 'SessionLocal', sessions)
    yield
    engine.dispose()
