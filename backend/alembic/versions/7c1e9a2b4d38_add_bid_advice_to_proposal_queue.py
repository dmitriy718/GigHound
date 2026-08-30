"""add bid_advice to proposal_queue (Phase 3.4)

Revision ID: 7c1e9a2b4d38
Revises: 0adef05255f0
Create Date: 2026-08-29 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '7c1e9a2b4d38'
down_revision: Union[str, None] = '0adef05255f0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('proposal_queue', sa.Column('bid_advice', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('proposal_queue', 'bid_advice')
