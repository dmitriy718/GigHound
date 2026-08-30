"""add proposal reply tracking (client_replied_at, save_as_template)

Revision ID: 0adef05255f0
Revises: 6d06a7fdf7dd
Create Date: 2026-08-29 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0adef05255f0'
down_revision: Union[str, None] = '6d06a7fdf7dd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('proposal_queue', sa.Column(
        'save_as_template', sa.Boolean(), server_default=sa.true(), nullable=False))
    op.add_column('proposal_queue', sa.Column(
        'client_replied_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('proposal_queue', 'client_replied_at')
    op.drop_column('proposal_queue', 'save_as_template')
