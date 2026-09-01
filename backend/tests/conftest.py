"""Shared test configuration.

Sets a JWT secret before any app module is imported (app.config reads env
at import time) so the real auth flow works under TestClient. Schema in
tests is created via Base.metadata.create_all on in-memory SQLite — no
Alembic needed. A fixed vault key is set so the credential vault works
without GIGHOUND_DEV_NOAUTH (which must stay off: auth tests rely on it).
"""
import os

os.environ.setdefault("GIGHOUND_SECRET_KEY", "test-secret-key")
os.environ.setdefault("GIGHOUND_WORKER_TOKEN", "test-worker-token")

# Isolate the suite from any live dev stack: the app and tests share
# localhost:6379 otherwise, and the running celery-beat's pacing locks /
# circuit-breaker keys (discovery:{user}:{platform}, circuit:*) collide with
# test user ids and make results timing-dependent. Hard-set (not setdefault)
# so it also wins over backend/.env via config.load_dotenv.
os.environ["REDIS_URL"] = "redis://localhost:6379/15"

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
    try:
        import redis

        r = redis.Redis.from_url(os.environ["REDIS_URL"], socket_timeout=1)
        r.ping()
        r.flushdb()
    except Exception:
        pass
    yield
