"""add_account_statuses_cleanup_policies

Revision ID: 67b08460ec49
Revises: edf81cf71744
Create Date: 2026-05-20 17:27:38.831345

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '67b08460ec49'
down_revision: str | Sequence[str] | None = 'edf81cf71744'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'account_statuses_cleanup_policies',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('account_id', sa.BigInteger(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('min_status_age', sa.Integer(), nullable=False, server_default='1209600'),
        sa.Column('keep_direct', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('keep_pinned', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('keep_polls', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('keep_media', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('keep_self_fav', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('keep_self_bookmark', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('min_favs', sa.Integer(), nullable=True),
        sa.Column('min_reblogs', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=False), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=False), nullable=False),
        sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('account_id'),
    )
    op.create_index(
        'index_account_statuses_cleanup_policies_on_account_id',
        'account_statuses_cleanup_policies',
        ['account_id'],
    )


def downgrade() -> None:
    op.drop_index('index_account_statuses_cleanup_policies_on_account_id')
    op.drop_table('account_statuses_cleanup_policies')
