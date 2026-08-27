"""add entry_time to journal entries

Revision ID: 8a2f5c3d1e09
Revises: 7d4a1e9c3f20
Create Date: 2026-08-27 13:59:00.000000+00:00
"""

from alembic import op
import sqlalchemy as sa

revision = "8a2f5c3d1e09"
down_revision = "7d4a1e9c3f20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("journal_entries", sa.Column("entry_time", sa.Time(), nullable=True))


def downgrade() -> None:
    op.drop_column("journal_entries", "entry_time")
