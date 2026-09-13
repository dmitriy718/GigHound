#!/usr/bin/env python3
"""Read-only migration/queue inventory; emits IDs and counts, never proposal text or credentials."""

import json
import os
from sqlalchemy import create_engine, text, inspect


def main():
    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as db:
        duplicates = db.execute(text("""SELECT job_id,count(*) FROM proposal_queue
            WHERE request_type='job' AND status NOT IN ('rejected','failed')
            GROUP BY job_id HAVING count(*)>1""")).all()
        states = db.execute(
            text("SELECT status,count(*) FROM proposal_queue GROUP BY status")
        ).all()
        accounts = db.execute(
            text(
                "SELECT user_id,platform,principal,count(*) FROM platform_accounts GROUP BY user_id,platform,principal HAVING count(*)>1"
            )
        ).all()
        columns = {c["name"] for c in inspect(db).get_columns("proposal_queue")}
        legacy = db.execute(
            text(
                "SELECT count(*) FROM proposal_queue WHERE status='approved'"
                + (
                    " AND approved_snapshot IS NULL"
                    if "approved_snapshot" in columns
                    else ""
                )
            )
        ).scalar()
        unbound = (
            db.execute(
                text(
                    "SELECT id FROM stealth_tasks WHERE status IN ('pending','claimed') AND (payload ->> 'account_id') IS NULL ORDER BY id LIMIT 1000"
                )
            )
            .scalars()
            .all()
        )
        print(
            json.dumps(
                {
                    "duplicate_live_job_ids": [r[0] for r in duplicates],
                    "proposal_states": dict(states),
                    "duplicate_account_identities": [list(r) for r in accounts],
                    "migration_safe": not duplicates and not accounts,
                    "approvals_requiring_snapshot_review": legacy,
                    "unbound_pending_claimed_task_ids_first_1000": unbound,
                    "worker_release_requires_review": bool(legacy or unbound),
                }
            )
        )
        if duplicates or accounts:
            raise SystemExit(2)


if __name__ == "__main__":
    main()
