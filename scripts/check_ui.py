#!/usr/bin/env python3
"""Run the real SPA/API journey against an owned temporary SQLite database.

Build frontend first. Run with backend Python; Playwright Python defaults to
worker/.venv/bin/python locally, or set GIGHOUND_UI_TEST_PYTHON in CI.
"""

from contextlib import ExitStack
from pathlib import Path
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]


def stop(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def main():
    with tempfile.TemporaryDirectory(
        prefix="gighound-ui-"
    ) as temporary, ExitStack() as stack:
        env = {
            **os.environ,
            "DATABASE_URL": f"sqlite:///{temporary}/ui.db",
            "REDIS_URL": "redis://127.0.0.1:1/13",
            "GIGHOUND_DEV_NOAUTH": "0",
            "GIGHOUND_SECRET_KEY": secrets.token_hex(32),
            "GIGHOUND_WORKER_TOKEN": secrets.token_hex(32),
            "GIGHOUND_WORKER_CREDENTIALS": "",
            "GIGHOUND_VAULT_KEY": Fernet.generate_key().decode(),
            "GIGHOUND_ALLOW_REGISTRATION": "true",
            "LLM_PROVIDER": "openai",
            "LLM_API_KEY": "",
            "LLM_BASE_URL": "http://127.0.0.1:1/v1",
            "GIGHOUND_FRONTEND_DIST": str(ROOT / "frontend/dist"),
        }
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT / "backend",
            env=env,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        for command, cwd, port in [
            (
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "app.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8059",
                ],
                ROOT / "backend",
                8059,
            ),
            (
                [
                    "npm",
                    "run",
                    "dev",
                    "--",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "4179",
                    "--strictPort",
                ],
                ROOT / "frontend",
                4179,
            ),
        ]:
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("127.0.0.1", port))
            log = stack.enter_context(open(Path(temporary) / f"{port}.log", "w+"))
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            stack.callback(stop, process)
            deadline = time.monotonic() + 30
            while True:
                if process.poll() is not None or time.monotonic() > deadline:
                    log.seek(0)
                    raise RuntimeError(f"UI test server failed: {log.read()}")
                try:
                    with urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
                        if response.status == 200:
                            break
                except Exception:
                    time.sleep(0.1)
        # Seed only this owned database; no provider is needed for a manual review journey.
        sys.path.insert(0, str(ROOT / "backend"))
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        from app.models import User, Job, ProposalQueueItem, PlatformAccount, AdapterState
        import bcrypt

        review_email = "review-" + secrets.token_hex(8) + "@example.test"
        review_password = secrets.token_hex(24)
        engine = create_engine(env["DATABASE_URL"])
        with Session(engine) as db:
            user = User(
                email=review_email,
                display_name="Synthetic reviewer",
                password_hash=bcrypt.hashpw(
                    review_password.encode(), bcrypt.gensalt()
                ).decode(),
            )
            db.add(user)
            db.flush()
            db.add(PlatformAccount(user_id=user.id,platform='freelancer',principal='polling',label='Synthetic polling account',mode='api'))
            for principal in ('agency-first','agency-second'):
                db.add(PlatformAccount(user_id=user.id,platform='upwork',principal=principal,label='Synthetic '+principal,mode='hybrid'))
            db.add(AdapterState(user_id=user.id,platform='upwork',key='agency_roster',value={'members':[{'username':'synthetic-legacy-member'}]}))
            for principal in ('first', 'second'):
                db.add(PlatformAccount(user_id=user.id, platform='guru', principal=principal, label='Synthetic '+principal, mode='api'))
            db.flush()
            job = Job(
                user_id=user.id,
                external_id="synthetic-review",
                platform="guru",
                title="Synthetic manual review",
                currency="EUR",
                job_type="fixed",
                quality_score=90,
            )
            db.add(job)
            db.flush()
            db.add(
                ProposalQueueItem(
                    user_id=user.id,
                    job_id=job.id,
                    platform="guru",
                    proposal_text="Original synthetic draft",
                    analysis={"tone": {"legacy": "malformed"}, "required_skills": ["Python", None]},
                    portfolio_match={"1": None, "2": {"title": {}, "overlap_pct": "bad", "matched_skills": ["Python", {}]}},
                    bid_amount=125,
                    status="pending_review",
                )
            )
            db.commit()
        engine.dispose()
        test_env = {
            **os.environ,
            "GIGHOUND_UI_REVIEW_EMAIL": review_email,
            "GIGHOUND_UI_REVIEW_PASSWORD": review_password,
        }
        local = ROOT / "worker/.venv/bin/python"
        python = os.environ.get(
            "GIGHOUND_UI_TEST_PYTHON", str(local) if local.exists() else sys.executable
        )
        for test in ["check_workbench.py", "check_alerts.py", "check_review.py", "check_drafts.py", "check_writing.py", "check_guided.py", "check_seller_accounts.py"]:
            subprocess.run(
                [python, str(ROOT / "frontend/tests" / test)],
                cwd=ROOT,
                check=True,
                env=test_env,
            )


if __name__ == "__main__":
    main()
