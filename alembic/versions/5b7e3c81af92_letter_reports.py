"""record report reasons alongside the counter

Revision ID: 5b7e3c81af92
Revises: 3f1c9a2b7de4
Create Date: 2026-08-22

Moderation needs to know *why* a letter was flagged, not just how often (P4).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '5b7e3c81af92'
down_revision: Union[str, None] = '3f1c9a2b7de4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'letter_reports',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('letter_id', sa.Uuid(), nullable=False),
        sa.Column('reason', sa.String(length=30), nullable=False),
        sa.Column('details', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
    )
    op.create_index('ix_letter_reports_letter_id', 'letter_reports', ['letter_id'])


def downgrade() -> None:
    op.drop_index('ix_letter_reports_letter_id', table_name='letter_reports')
    op.drop_table('letter_reports')
