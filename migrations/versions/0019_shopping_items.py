"""Модуль «Покупки»: общий список покупок семьи (тикет #84).

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0019'
down_revision: Union[str, None] = '0018'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # База, развёрнутая `create_all()` без штампа головы, таблицу уже содержит.
    if 'shopping_items' in sa.inspect(op.get_bind()).get_table_names():
        return

    op.create_table(
        'shopping_items',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('family_id', sa.Integer(),
                  sa.ForeignKey('families.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('text', sa.String(length=255), nullable=False),
        sa.Column('added_by', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('checked', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('checked_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('shopping_items')
