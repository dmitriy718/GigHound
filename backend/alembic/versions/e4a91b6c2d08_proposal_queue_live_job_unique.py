"""partial unique index: one live generated proposal per job

Guards the select-then-insert race in generation_gates_pass — concurrent
generation for the same job now loses with an IntegrityError instead of
double-inserting. rejected/failed rows may pile up; follow_up/buyer_request
rows share job_id legitimately, so the predicate is request_type = 'job'.

Revision ID: e4a91b6c2d08
Revises: d3f8a2c91e07
Create Date: 2026-09-02 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e4a91b6c2d08'
down_revision: Union[str, None] = 'd3f8a2c91e07'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PREDICATE = sa.text("status NOT IN ('rejected','failed') AND request_type = 'job'")


def upgrade() -> None:
    op.create_index('uq_proposal_queue_live_job', 'proposal_queue', ['job_id'],
                    unique=True,
                    sqlite_where=_PREDICATE,
                    postgresql_where=_PREDICATE)


def downgrade() -> None:
    op.drop_index('uq_proposal_queue_live_job', table_name='proposal_queue')
