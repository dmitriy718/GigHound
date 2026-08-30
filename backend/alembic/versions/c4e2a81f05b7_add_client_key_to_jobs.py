"""add client_key to jobs (indexed client identity for keyed history lookups)

Revision ID: c4e2a81f05b7
Revises: b3f1c9d42e10
Create Date: 2026-08-29 16:00:00.000000

Populated at write time from client_info+platform (before_insert/before_update
listener in models.py). No backfill: NULL = no history.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c4e2a81f05b7'
down_revision: Union[str, None] = 'b3f1c9d42e10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('jobs', sa.Column('client_key', sa.String(length=160), nullable=True))
    op.create_index('ix_jobs_client_key', 'jobs', ['client_key'])


def downgrade() -> None:
    op.drop_index('ix_jobs_client_key', table_name='jobs')
    op.drop_column('jobs', 'client_key')
