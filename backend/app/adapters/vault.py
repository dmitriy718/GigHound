"""Credential vault: Fernet-encrypted, per-user/per-platform/per-principal secret storage.

The encryption key comes from the GIGHOUND_VAULT_KEY env var (a urlsafe
base64 32-byte key; GIGHUNTER_VAULT_KEY is accepted as a legacy alias).
Outside explicit dev mode (GIGHOUND_DEV_NOAUTH=1) the key is MANDATORY —
first vault use fails fast with a RuntimeError. In dev mode an ephemeral
key is generated and persisted to backend/.vault-dev-key (mode 0600,
gitignored) so restarts and workers share it.

Generate a key with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
import json
import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from ..models import AdapterCredential, AdapterState
from .base import AdapterAuthError

log = logging.getLogger(__name__)

_DEV_KEY_FILE = Path(__file__).resolve().parents[2] / ".vault-dev-key"


def _dev_key() -> str:
    """Dev-only key, persisted to disk so restarts/workers share it."""
    try:
        return _DEV_KEY_FILE.read_text().strip()
    except FileNotFoundError:
        pass
    key = Fernet.generate_key().decode()
    try:
        fd = os.open(_DEV_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:  # another process won the race — use its key
        return _DEV_KEY_FILE.read_text().strip()
    try:
        os.write(fd, key.encode())
    finally:
        os.close(fd)
    log.warning(
        "GIGHOUND_VAULT_KEY not set — generated a dev-only key at %s; "
        "set GIGHOUND_VAULT_KEY for anything beyond local development",
        _DEV_KEY_FILE,
    )
    return key


def _fernet() -> Fernet:
    key = os.getenv("GIGHOUND_VAULT_KEY") or os.getenv("GIGHUNTER_VAULT_KEY")
    if not key:
        if os.getenv("GIGHOUND_DEV_NOAUTH") != "1":
            raise RuntimeError(
                "GIGHOUND_VAULT_KEY is not set — refusing to use the credential "
                "vault without an encryption key. Generate one with: python -c "
                "\"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\" "
                "or set GIGHOUND_DEV_NOAUTH=1 for local development."
            )
        key = _dev_key()
    return Fernet(key.encode() if isinstance(key, str) else key)


class CredentialVault:
    """Stores and retrieves encrypted credential dicts per user+platform+principal.

    Rows are tenant-owned: a vault instance only ever sees the credentials
    of the user it was created for (AD-1).
    """

    def __init__(self, db: Session, user_id: int):
        self.db = db
        self.user_id = user_id

    def store(self, platform: str, principal: str, secrets: dict):
        blob = _fernet().encrypt(json.dumps(secrets).encode()).decode()
        row = (
            self.db.query(AdapterCredential)
            .filter_by(user_id=self.user_id, platform=platform, principal=principal)
            .first()
        )
        if row:
            row.blob = blob
        else:
            row = AdapterCredential(user_id=self.user_id, platform=platform,
                                    principal=principal, blob=blob)
            self.db.add(row)
        self.db.commit()

    def load(self, platform: str, principal: str) -> dict | None:
        row = (
            self.db.query(AdapterCredential)
            .filter_by(user_id=self.user_id, platform=platform, principal=principal)
            .first()
        )
        if not row:
            return None
        try:
            return json.loads(_fernet().decrypt(row.blob.encode()).decode())
        except InvalidToken:
            raise AdapterAuthError(
                "stored credentials unreadable — re-enroll credentials"
            ) from None

    def delete(self, platform: str, principal: str):
        self.db.query(AdapterCredential).filter_by(
            user_id=self.user_id, platform=platform, principal=principal
        ).delete()
        self.db.commit()


class StateStore:
    """Operational key-value state per adapter (bid quotas, cursors...).

    Tenant-scoped the same way as the vault.
    """

    def __init__(self, db: Session, user_id: int):
        self.db = db
        self.user_id = user_id

    def get(self, platform: str, key: str, default=None):
        row = (self.db.query(AdapterState)
               .filter_by(user_id=self.user_id, platform=platform, key=key)
               .first())
        return row.value if row else default

    def set(self, platform: str, key: str, value: dict):
        row = (self.db.query(AdapterState)
               .filter_by(user_id=self.user_id, platform=platform, key=key)
               .first())
        if row:
            row.value = value
        else:
            self.db.add(AdapterState(user_id=self.user_id, platform=platform,
                                     key=key, value=value))
        self.db.commit()
