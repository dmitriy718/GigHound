import os

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://gighound:gighound@localhost:5432/gighound",
)
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")
CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "300"))

# --- Auth (AD-2) ---
SECRET_KEY = os.getenv("GIGHOUND_SECRET_KEY")
# Dev escape hatch: single implicit user, no JWT required. Never set in prod.
DEV_NOAUTH = os.getenv("GIGHOUND_DEV_NOAUTH") == "1"
ALLOW_REGISTRATION = os.getenv("GIGHOUND_ALLOW_REGISTRATION", "true").lower() in (
    "1", "true", "yes",
)

# --- Stealth worker (AD-4) ---
# Shared deployment-level token authenticating the browser worker pool.
# Separate from user JWTs; mandatory outside dev (see auth.validate_auth_config).
WORKER_TOKEN = os.getenv("GIGHOUND_WORKER_TOKEN")
