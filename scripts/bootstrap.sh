#!/usr/bin/env bash
# GigHound first-boot bootstrap.
#
# Creates .env from .env.example (when missing) and fills the deployment
# secrets: GIGHOUND_SECRET_KEY, GIGHOUND_VAULT_KEY (Fernet format),
# GIGHOUND_WORKER_TOKEN, POSTGRES_PASSWORD.
#
# Idempotent: any key that already has a non-empty, non-placeholder value is
# left untouched — re-running never rotates secrets. The one placeholder we
# DO replace is POSTGRES_PASSWORD=gighound, the shipped .env.example default.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/.env"
EXAMPLE="$ROOT/.env.example"

if [[ ! -f "$ENV_FILE" ]]; then
  cp "$EXAMPLE" "$ENV_FILE"
  echo "created .env from .env.example"
else
  echo ".env already exists — filling only missing/placeholder secrets"
fi
# .env holds DB credentials and signing keys — never leave it world-readable
# (cp/sed would otherwise inherit the umask, often 644).
chmod 600 "$ENV_FILE"

rand_hex() { # rand_hex <bytes>
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex "$1"
  else
    python3 -c "import secrets; print(secrets.token_hex($1))"
  fi
}

fernet_key() {
  # a Fernet key is the urlsafe-base64 encoding of 32 random bytes
  if command -v python3 >/dev/null 2>&1; then
    python3 -c "import secrets, base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
  else
    openssl rand -base64 32 | tr '+/' '-_'
  fi
}

# fill <KEY> <VALUE> [PLACEHOLDER] — set KEY=VALUE in .env unless KEY already
# holds a real value (non-empty and not PLACEHOLDER).
fill() {
  local key="$1" value="$2" placeholder="${3:-}" current
  current="$(grep -E "^${key}=" "$ENV_FILE" | head -1 | cut -d= -f2- || true)"
  if [[ -n "$current" && "$current" != "$placeholder" ]]; then
    echo "  $key: already set — leaving as-is"
    return
  fi
  # sed -i.bak + rm is portable across GNU and BSD/macOS (plain `sed -i` is not)
  if grep -qE "^# *${key}=" "$ENV_FILE"; then
    sed -i.bak "s|^# *${key}=.*|${key}=${value}|" "$ENV_FILE" && rm -f "$ENV_FILE.bak"
  elif grep -qE "^${key}=" "$ENV_FILE"; then
    sed -i.bak "s|^${key}=.*|${key}=${value}|" "$ENV_FILE" && rm -f "$ENV_FILE.bak"
  else
    # appending onto a file without a trailing newline would corrupt the last line
    [[ -n "$(tail -c 1 "$ENV_FILE")" ]] && printf '\n' >> "$ENV_FILE"
    printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
  fi
  chmod 600 "$ENV_FILE"  # sed -i recreates the file — re-tighten after edits
  echo "  $key: generated"
}

fill GIGHOUND_SECRET_KEY "$(rand_hex 32)"
fill GIGHOUND_VAULT_KEY "$(fernet_key)"
fill GIGHOUND_WORKER_TOKEN "$(rand_hex 32)"
fill POSTGRES_PASSWORD "$(rand_hex 16)" "gighound"

cat <<'EOF'

Done. Next steps:

  docker compose up --build -d                                   # db, redis, backend, celery
  docker compose exec backend python -m scripts.seed_defaults    # demo data (optional, DEV ONLY —
                                                                 # published credentials; the seed
                                                                 # refuses when GIGHOUND_ENV=production)
  open http://localhost:8000   # dev-only demo login: demo@gighound.local / demo1234

Notes:
  - The backend container runs `alembic upgrade head` on startup.
  - DATABASE_URL in .env is for LOCAL development only; the compose stack
    builds its own from POSTGRES_PASSWORD.
  - Optional local LLM: docker compose --profile llm up -d && \
      docker compose exec ollama ollama pull qwen3:4b
EOF
