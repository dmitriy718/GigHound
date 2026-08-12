"""Credential vault: Fernet-encrypted, per-platform/per-principal secret storage.

The encryption key comes from the GIGHOUND_VAULT_KEY env var (a urlsafe
base64 32-byte key; GIGHUNTER_VAULT_KEY is accepted as a legacy alias).
If unset, an ephemeral key is generated — fine for dev, but secrets will
not survive restarts, so a warning is logged.

Generate a key with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
import json
import logging
import os

from cryptography.fernet import Fernet
from sqlalchemy.orm import Session

from ..models import AdapterCredential, AdapterState

log = logging.getLogger(__name__)


def _fernet() -> Fernet:
    key = os.getenv("GIGHOUND_VAULT_KEY") or os.getenv("GIGHUNTER_VAULT_KEY")
    if not key:
        key = _fernet.__dict__.get("_ephemeral")
        if not key:
            key = Fernet.generate_key().decode()
            _fernet.__dict__["_ephemeral"] = key
            log.warning(
                "GIGHOUND_VAULT_KEY not set — using ephemeral key; "
                "stored credentials will NOT survive restarts"
            )
    return Fernet(key.encode() if isinstance(key, str) else key)


class CredentialVault:
    """Stores and retrieves encrypted credential dicts per platform+principal."""

    def __init__(self, db: Session):
        self.db = db

    def store(self, platform: str, principal: str, secrets: dict):
        blob = _fernet().encrypt(json.dumps(secrets).encode()).decode()
        row = (
            self.db.query(AdapterCredential)
            .filter_by(platform=platform, principal=principal)
            .first()
        )
        if row:
            row.blob = blob
        else:
            row = AdapterCredential(platform=platform, principal=principal, blob=blob)
            self.db.add(row)
        self.db.commit()

    def load(self, platform: str, principal: str) -> dict | None:
        row = (
            self.db.query(AdapterCredential)
            .filter_by(platform=platform, principal=principal)
            .first()
        )
        if not row:
            return None
        return json.loads(_fernet().decrypt(row.blob.encode()).decode())

    def delete(self, platform: str, principal: str):
        self.db.query(AdapterCredential).filter_by(
            platform=platform, principal=principal
        ).delete()
        self.db.commit()


class StateStore:
    """Operational key-value state per adapter (bid quotas, cursors...)."""

    def __init__(self, db: Session):
        self.db = db

    def get(self, platform: str, key: str, default=None):
        row = self.db.query(AdapterState).filter_by(platform=platform, key=key).first()
        return row.value if row else default

    def set(self, platform: str, key: str, value: dict):
        row = self.db.query(AdapterState).filter_by(platform=platform, key=key).first()
        if row:
            row.value = value
        else:
            self.db.add(AdapterState(platform=platform, key=key, value=value))
        self.db.commit()
