"""letter lifecycle, capability tokens, suppression list and delivery log

Revision ID: 3f1c9a2b7de4
Revises: 224647aa797e
Create Date: 2026-08-21

Replaces the boolean is_sent with an explicit status so the lifecycle has a
single source of truth, and adds the columns behind confirmation (P1),
cancellation (P2), timezone-correct delivery (P3), moderation (P4),
unsubscribes (P5), retry bookkeeping (R4) and the delivery audit log (P7).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '3f1c9a2b7de4'
down_revision: Union[str, None] = '224647aa797e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('letters', sa.Column('timezone_name', sa.String(length=64),
                                       nullable=False, server_default='UTC'))
    op.add_column('letters', sa.Column('status', sa.String(length=20),
                                       nullable=False, server_default='pending'))
    op.add_column('letters', sa.Column('attempts', sa.Integer(),
                                       nullable=False, server_default='0'))
    op.add_column('letters', sa.Column('last_error', sa.Text(), nullable=True))
    op.add_column('letters', sa.Column('confirm_token_hash', sa.String(length=64), nullable=True))
    op.add_column('letters', sa.Column('manage_token_hash', sa.String(length=64), nullable=True))
    op.add_column('letters', sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('letters', sa.Column('reported_count', sa.Integer(),
                                       nullable=False, server_default='0'))
    op.add_column('letters', sa.Column('is_hidden', sa.Boolean(),
                                       nullable=False, server_default='false'))

    # Existing letters predate the confirmation flow. Anything already
    # delivered becomes 'sent'; anything still queued becomes 'scheduled'
    # rather than 'pending', so it is not stranded awaiting a confirmation
    # email that was never sent.
    op.execute("UPDATE letters SET status = CASE WHEN is_sent THEN 'sent' ELSE 'scheduled' END")

    op.create_index('ix_letters_status', 'letters', ['status'])
    op.create_index('ix_letters_confirm_token_hash', 'letters', ['confirm_token_hash'])
    op.create_index('ix_letters_manage_token_hash', 'letters', ['manage_token_hash'])

    op.drop_index('ix_letters_is_sent', table_name='letters')
    op.drop_column('letters', 'is_sent')

    op.create_table(
        'email_suppressions',
        sa.Column('email', sa.String(length=320), primary_key=True),
        sa.Column('reason', sa.String(length=50), nullable=False, server_default='unsubscribed'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
    )

    op.create_table(
        'delivery_logs',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('letter_id', sa.Uuid(), nullable=False),
        sa.Column('outcome', sa.String(length=20), nullable=False),
        sa.Column('detail', sa.Text(), nullable=True),
        sa.Column('attempted_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
    )
    op.create_index('ix_delivery_logs_letter_id', 'delivery_logs', ['letter_id'])

    _sanitize_existing_bodies()


def _sanitize_existing_bodies() -> None:
    """
    Clean letters written before bodies were sanitized on write (E4).

    Rows already in the table can carry script tags and event handlers, and
    the public feed renders them as markup — so sanitizing new writes alone
    would leave the existing stored XSS in place.
    """
    import nh3

    from app.core.security import ALLOWED_ATTRS, ALLOWED_TAGS

    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, body FROM letters")).fetchall()
    for row_id, body in rows:
        if not body:
            continue
        cleaned = nh3.clean(
            body,
            tags=ALLOWED_TAGS,
            attributes=ALLOWED_ATTRS,
            url_schemes={"http", "https", "mailto"},
            link_rel="noopener noreferrer nofollow",
        )
        if cleaned != body:
            conn.execute(
                sa.text("UPDATE letters SET body = :body WHERE id = :id"),
                {"body": cleaned, "id": row_id},
            )


def downgrade() -> None:
    op.drop_index('ix_delivery_logs_letter_id', table_name='delivery_logs')
    op.drop_table('delivery_logs')
    op.drop_table('email_suppressions')

    op.add_column('letters', sa.Column('is_sent', sa.Boolean(),
                                       nullable=False, server_default='false'))
    op.execute("UPDATE letters SET is_sent = (status = 'sent')")
    op.create_index('ix_letters_is_sent', 'letters', ['is_sent'])

    op.drop_index('ix_letters_manage_token_hash', table_name='letters')
    op.drop_index('ix_letters_confirm_token_hash', table_name='letters')
    op.drop_index('ix_letters_status', table_name='letters')

    for col in ('is_hidden', 'reported_count', 'confirmed_at', 'manage_token_hash',
                'confirm_token_hash', 'last_error', 'attempts', 'status', 'timezone_name'):
        op.drop_column('letters', col)
