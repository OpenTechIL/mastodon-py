"""add_user_roles

Revision ID: a1b2c3d4e5f6
Revises: 67b08460ec49
Create Date: 2026-05-20 18:00:00.000000

"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
import sqlalchemy.dialects.postgresql


revision: str = 'a1b2c3d4e5f6'
down_revision: str | Sequence[str] | None = '67b08460ec49'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('users', sa.Column('chosen_languages', sa.dialects.postgresql.ARRAY(sa.String()), nullable=True))
    op.create_table(
        'user_roles',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('name', sa.String(), nullable=False, server_default=''),
        sa.Column('color', sa.String(), nullable=False, server_default=''),
        sa.Column('position', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('permissions', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('highlighted', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('created_at', sa.DateTime(timezone=False), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=False), nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_foreign_key(
        'users_role_id_fkey',
        'users', 'user_roles',
        ['role_id'], ['id'],
    )


def downgrade() -> None:
    op.drop_constraint('users_role_id_fkey', 'users', type_='foreignkey')
    op.drop_table('user_roles')
    op.drop_column('users', 'chosen_languages')
