"""Векторы записей для поиска по смыслу (тикет #82, ADR-0017).

float32-блобы в обычной таблице — одинаково в SQLite и Postgres, без
нативных расширений; косинус считается в Python по маленькому корпусу семьи.

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0018'
down_revision: Union[str, None] = '0017'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # База, развёрнутая `create_all()` без штампа головы, таблицу уже содержит.
    if 'board_entry_embeddings' in sa.inspect(op.get_bind()).get_table_names():
        return

    op.create_table(
        'board_entry_embeddings',
        sa.Column('entry_id', sa.Integer(),
                  sa.ForeignKey('board_entries.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('model', sa.String(length=128), nullable=False),
        sa.Column('vector', sa.LargeBinary(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('board_entry_embeddings')
