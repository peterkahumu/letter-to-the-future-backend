"""Create letters table

Revision ID: 001
Revises:
Create Date: 2024-01-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "letters",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("recipient_email", sa.String(length=320), nullable=False),
        sa.Column("sender_name", sa.String(length=150), nullable=False),
        sa.Column("subject", sa.String(length=500), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("deliver_at", sa.Date(), nullable=False),
        sa.Column("is_sent", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_letters_recipient_email", "letters", ["recipient_email"])
    op.create_index("ix_letters_deliver_at", "letters", ["deliver_at"])
    op.create_index("ix_letters_is_sent", "letters", ["is_sent"])


def downgrade() -> None:
    op.drop_index("ix_letters_is_sent", table_name="letters")
    op.drop_index("ix_letters_deliver_at", table_name="letters")
    op.drop_index("ix_letters_recipient_email", table_name="letters")
    op.drop_table("letters")
