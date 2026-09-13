#!/usr/bin/env python3
"""Encrypted PostgreSQL + matching vault-key backup and empty-database restore.

Run with the backend Python environment. Secrets come from environment only.
No shell execution, passwords on argv, or unencrypted final backup artifacts.
"""

import argparse
from contextlib import closing
import base64
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import tarfile
import tempfile
from urllib.parse import urlsplit, unquote
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.fernet import Fernet

MAGIC = b"GIGHOUND-BACKUP-1\x00"
MAX_DUMP_BYTES = 256 * 1024 * 1024


def pg_environment(url):
    parsed = urlsplit(url.replace("postgresql+psycopg2:", "postgresql:"))
    if (
        parsed.scheme not in ("postgresql", "postgres")
        or not parsed.hostname
        or not parsed.path.strip("/")
    ):
        raise ValueError("a PostgreSQL database URL is required")
    env = dict(os.environ)
    env.update(
        PGHOST=parsed.hostname,
        PGPORT=str(parsed.port or 5432),
        PGUSER=unquote(parsed.username or ""),
        PGPASSWORD=unquote(parsed.password or ""),
        PGDATABASE=unquote(parsed.path.strip("/")),
    )
    return env


def key(salt):
    phrase = os.environ.get("GIGHOUND_BACKUP_PASSPHRASE", "")
    if len(phrase) < 20:
        raise ValueError(
            "GIGHOUND_BACKUP_PASSPHRASE must contain at least 20 characters"
        )
    return PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=600000
    ).derive(phrase.encode())


def private_write(path, content):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as file:
        file.write(content)


def backup(path):
    env = pg_environment(os.environ["DATABASE_URL"])
    vault = os.environ["GIGHOUND_VAULT_KEY"].encode()
    Fernet(vault)  # reject an unusable key before starting a database backup
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    cipher = AESGCM(key(salt))
    # Custom-format dumps can be inspected/restored using standard PostgreSQL tooling.
    with tempfile.TemporaryFile() as dump_file:
        subprocess.run(
            ["pg_dump", "--format=custom", "--no-owner", "--no-acl"],
            env=env,
            check=True,
            stdout=dump_file,
            stderr=subprocess.PIPE,
        )
        if dump_file.tell() > MAX_DUMP_BYTES:
            raise ValueError(
                "dump exceeds the 256 MiB in-memory encryption limit; use an operator-managed streaming backup"
            )
        dump_file.seek(0)
        dump = dump_file.read()
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        for name, content in [("database.dump", dump), ("vault.key", vault)]:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o600
            tar.addfile(info, io.BytesIO(content))
    private_write(
        path, MAGIC + salt + nonce + cipher.encrypt(nonce, archive.getvalue(), MAGIC)
    )
    return {
        "encrypted_backup": str(path),
        "database_bytes": len(dump),
        "vault_key_included": True,
    }


def unpack(path):
    if Path(path).stat().st_size > MAX_DUMP_BYTES + 1024 * 1024:
        raise ValueError("backup exceeds the supported 256 MiB dump limit")
    content = Path(path).read_bytes()
    if not content.startswith(MAGIC):
        raise ValueError("unsupported backup format")
    start = len(MAGIC)
    salt = content[start : start + 16]
    nonce = content[start + 16 : start + 28]
    data = AESGCM(key(salt)).decrypt(nonce, content[start + 28 :], MAGIC)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
        if sorted(tar.getnames()) != ["database.dump", "vault.key"]:
            raise ValueError("unexpected archive members")
        result = {name: tar.extractfile(name).read() for name in tar.getnames()}
    Fernet(result["vault.key"])
    return result


def restore(path, confirm_db, key_output):
    import psycopg2

    env = pg_environment(os.environ["DATABASE_URL"])
    if confirm_db != env["PGDATABASE"]:
        raise ValueError("--confirm-db must exactly match the target database name")
    if Path(key_output).exists():
        raise ValueError("vault key output already exists")
    payload = unpack(path)
    with closing(
        psycopg2.connect(
            host=env["PGHOST"],
            port=env["PGPORT"],
            user=env["PGUSER"],
            password=env["PGPASSWORD"],
            dbname=env["PGDATABASE"],
        )
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog','information_schema') AND table_schema NOT LIKE 'pg_toast%'"
            )
            if cursor.fetchone()[0]:
                raise ValueError("restore requires an empty target database")
    subprocess.run(
        [
            "pg_restore",
            "--single-transaction",
            "--exit-on-error",
            "--no-owner",
            "--no-acl",
            "--dbname",
            env["PGDATABASE"],
        ],
        input=payload["database.dump"],
        env=env,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    private_write(key_output, payload["vault.key"])
    return {"restored_database": confirm_db, "vault_key_file": str(key_output)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["backup", "verify", "restore"])
    parser.add_argument("path", type=Path)
    parser.add_argument("--confirm-db")
    parser.add_argument("--key-output", type=Path)
    args = parser.parse_args()
    if args.action == "backup":
        result = backup(args.path)
    elif args.action == "verify":
        payload = unpack(args.path)
        result = {
            "authenticated_encryption_valid": True,
            "database_bytes": len(payload["database.dump"]),
            "vault_key_valid": True,
        }
    else:
        if not args.confirm_db or not args.key_output:
            parser.error("restore requires --confirm-db and --key-output")
        result = restore(args.path, args.confirm_db, args.key_output)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
