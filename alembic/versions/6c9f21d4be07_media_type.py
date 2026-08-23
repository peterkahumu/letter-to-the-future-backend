"""record what kind of media an attachment is

Revision ID: 6c9f21d4be07
Revises: 5b7e3c81af92
Create Date: 2026-08-23

Attachments were all rendered as images. Storing the kind lets audio render as
audio and video as video. Existing rows are backfilled from the file extension.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '6c9f21d4be07'
down_revision: Union[str, None] = '5b7e3c81af92'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('letters', sa.Column('media_type', sa.String(length=20), nullable=True))

    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, media_url FROM letters WHERE media_url IS NOT NULL")
    ).fetchall()

    from app.core.media import kind_from_url

    for row_id, url in rows:
        kind = kind_from_url(url)
        if kind:
            conn.execute(
                sa.text("UPDATE letters SET media_type = :k WHERE id = :i"),
                {"k": kind, "i": row_id},
            )


def downgrade() -> None:
    op.drop_column('letters', 'media_type')
