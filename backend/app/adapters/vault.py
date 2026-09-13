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

from ..models import AdapterCredential, AdapterState, PlatformAccount, User
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

    def __init__(self, db: Session, user_id: int, *, account_id: int | None = None, account_epoch: str | None = None):
        self.db = db
        self.user_id = user_id
        self.account_id = account_id
        self.account_epoch = account_epoch
        if account_id is not None and account_epoch is None:
            self.account_epoch = db.query(PlatformAccount.identity_epoch).filter_by(id=account_id, user_id=user_id).scalar()
        self._observed = {}

    def observe(self, platform: str, principal: str):
        """Capture a credential version before an external token exchange."""
        row = self.db.query(AdapterCredential).filter_by(
            user_id=self.user_id, platform=platform, principal=principal
        ).populate_existing().first()
        self._observed[(platform, principal)] = row.blob if row else None
        return row

    def _lock_owner(self):
        owner = self.db.query(User).filter_by(id=self.user_id).populate_existing().with_for_update().one_or_none()
        if owner is None or not owner.is_active:
            raise AdapterAuthError("credential owner is no longer active")

    def store(self, platform: str, principal: str, secrets: dict):
        self._lock_owner()
        if self.account_id is not None and not self.db.query(PlatformAccount.id).filter_by(
            id=self.account_id, user_id=self.user_id, platform=platform, principal=principal, identity_epoch=self.account_epoch
        ).first():
            raise AdapterAuthError("enrollment account was removed; start again")
        blob = _fernet().encrypt(json.dumps(secrets).encode()).decode()
        row = (
            self.db.query(AdapterCredential)
            .filter_by(user_id=self.user_id, platform=platform, principal=principal)
            .populate_existing()
            .first()
        )
        key = (platform, principal)
        if key in self._observed and self._observed[key] != (row.blob if row else None):
            raise AdapterAuthError("credentials changed during token exchange; start again")
        if row:
            row.blob = blob
        else:
            row = AdapterCredential(user_id=self.user_id, platform=platform,
                                    principal=principal, blob=blob)
            self.db.add(row)
        self.db.commit()
        self._observed[key] = blob

    def load(self, platform: str, principal: str) -> dict | None:
        row = self.observe(platform, principal)
        if not row:
            return None
        try:
            return json.loads(_fernet().decrypt(row.blob.encode()).decode())
        except InvalidToken:
            raise AdapterAuthError(
                "stored credentials unreadable — re-enroll credentials"
            ) from None

    def delete(self, platform: str, principal: str):
        self._lock_owner()
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
        row = (self.db.query(AdapterState.value)
               .filter_by(user_id=self.user_id, platform=platform, key=key)
               .first())
        return row[0] if row else default

    def set(self, platform: str, key: str, value: dict):
        # Cursor/roster writers can start in different processes with no row yet.
        if self.db.get_bind().dialect.name == 'postgresql':
            from sqlalchemy.dialects.postgresql import insert
        else:
            from sqlalchemy.dialects.sqlite import insert
        from datetime import datetime, timezone
        statement = insert(AdapterState).values(user_id=self.user_id,platform=platform,key=key,value=value)
        self.db.execute(statement.on_conflict_do_update(
            index_elements=['user_id','platform','key'],set_={'value':value,'updated_at':datetime.now(timezone.utc)}))
        self.db.commit()
