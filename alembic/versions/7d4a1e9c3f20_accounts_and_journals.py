"""user accounts, sessions, journals, and journal entries

Revision ID: 7d4a1e9c3f20
Revises: 6c9f21d4be07
Create Date: 2026-08-27

Adds accounts (E6) and the journal side of the app.

`letters.user_id` is nullable on purpose: every letter written before this
migration was written by a guest, and guests can still write. ON DELETE SET
NULL, not CASCADE — deleting an account must not cancel mail a recipient is
already waiting for.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '7d4a1e9c3f20'
down_revision: Union[str, None] = '6c9f21d4be07'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'users',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('email', sa.String(length=320), nullable=False),
        sa.Column('password_hash', sa.String(length=128), nullable=False),
        sa.Column('display_name', sa.String(length=150), nullable=False, server_default=''),
        sa.Column('role', sa.String(length=20), nullable=False, server_default='user'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('is_verified', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('verify_token_hash', sa.String(length=64), nullable=True),
        sa.Column('reset_token_hash', sa.String(length=64), nullable=True),
        sa.Column('reset_token_expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
    )
    # Unique, not just indexed — this constraint is what stops two accounts
    # existing for the same address under a race.
    op.create_index('ix_users_email', 'users', ['email'], unique=True)
    op.create_index('ix_users_role', 'users', ['role'])
    op.create_index('ix_users_verify_token_hash', 'users', ['verify_token_hash'])
    op.create_index('ix_users_reset_token_hash', 'users', ['reset_token_hash'])

    op.create_table(
        'refresh_tokens',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoked_reason', sa.String(length=20), nullable=True),
        sa.Column('user_agent', sa.String(length=300), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    )
    op.create_index('ix_refresh_tokens_token_hash', 'refresh_tokens', ['token_hash'], unique=True)
    op.create_index('ix_refresh_tokens_user_id', 'refresh_tokens', ['user_id'])

    op.create_table(
        'journals',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('description', sa.String(length=1000), nullable=True),
        sa.Column('color', sa.String(length=20), nullable=False, server_default='turquoise'),
        sa.Column('is_archived', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    )
    op.create_index('ix_journals_user_id', 'journals', ['user_id'])

    op.create_table(
        'journal_entries',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('journal_id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('title', sa.String(length=300), nullable=False, server_default=''),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('entry_date', sa.Date(), nullable=False),
        sa.Column('mood', sa.String(length=30), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['journal_id'], ['journals.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    )
    op.create_index('ix_journal_entries_journal_id', 'journal_entries', ['journal_id'])
    op.create_index('ix_journal_entries_user_id', 'journal_entries', ['user_id'])
    op.create_index('ix_journal_entries_entry_date', 'journal_entries', ['entry_date'])
    # The dashboard's "my diary, newest first" query, and every per-journal
    # listing, sort on this pair. Without it both degrade to a sort of the
    # whole table once someone has a few years of entries.
    op.create_index(
        'ix_journal_entries_user_date', 'journal_entries', ['user_id', 'entry_date']
    )

    op.add_column('letters', sa.Column('user_id', sa.Uuid(), nullable=True))
    op.create_index('ix_letters_user_id', 'letters', ['user_id'])
    op.create_foreign_key(
        'fk_letters_user_id', 'letters', 'users', ['user_id'], ['id'], ondelete='SET NULL'
    )


def downgrade() -> None:
    op.drop_constraint('fk_letters_user_id', 'letters', type_='foreignkey')
    op.drop_index('ix_letters_user_id', table_name='letters')
    op.drop_column('letters', 'user_id')

    op.drop_index('ix_journal_entries_user_date', table_name='journal_entries')
    op.drop_index('ix_journal_entries_entry_date', table_name='journal_entries')
    op.drop_index('ix_journal_entries_user_id', table_name='journal_entries')
    op.drop_index('ix_journal_entries_journal_id', table_name='journal_entries')
    op.drop_table('journal_entries')

    op.drop_index('ix_journals_user_id', table_name='journals')
    op.drop_table('journals')

    op.drop_index('ix_refresh_tokens_user_id', table_name='refresh_tokens')
    op.drop_index('ix_refresh_tokens_token_hash', table_name='refresh_tokens')
    op.drop_table('refresh_tokens')

    op.drop_index('ix_users_reset_token_hash', table_name='users')
    op.drop_index('ix_users_verify_token_hash', table_name='users')
    op.drop_index('ix_users_role', table_name='users')
    op.drop_index('ix_users_email', table_name='users')
    op.drop_table('users')
