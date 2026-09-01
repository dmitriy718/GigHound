"""add stealth task reclaim_count (reaper resets dead-worker claims)

Revision ID: d3f8a2c91e07
Revises: c4e2a81f05b7
Create Date: 2026-09-01 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd3f8a2c91e07'
down_revision: Union[str, None] = 'c4e2a81f05b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('stealth_tasks', sa.Column(
        'reclaim_count', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    op.drop_column('stealth_tasks', 'reclaim_count')
