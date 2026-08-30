"""add stealth task claim columns (claimed_by, claimed_at)

Revision ID: b3f1c9d42e10
Revises: 0adef05255f0
Create Date: 2026-08-29 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b3f1c9d42e10'
down_revision: Union[str, None] = '7c1e9a2b4d38'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('stealth_tasks', sa.Column(
        'claimed_by', sa.String(length=200), nullable=True))
    op.add_column('stealth_tasks', sa.Column(
        'claimed_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('stealth_tasks', 'claimed_at')
    op.drop_column('stealth_tasks', 'claimed_by')
