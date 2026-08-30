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

from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("GIGHOUND_VAULT_KEY", Fernet.generate_key().decode())
