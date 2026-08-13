"""Словарь типов доски и события, извлечённые из записей (тикет #30, спека #19).

Событие каскадится от записи: правка записи переразбирает её события, удаление
уносит. Словарь типов каскадится от доски — «кормление» одной доски ничего не
значит для другой.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # База, развёрнутая `create_all()` без штампа головы, уже содержит таблицы
    # из моделей — довозить нечего.
    if "board_events" in sa.inspect(op.get_bind()).get_table_names():
        return

    op.create_table('board_event_types',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('board_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('unit', sa.String(length=16), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['board_id'], ['boards.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('board_id', 'name', name='uq_board_event_type')
    )
    with op.batch_alter_table('board_event_types', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_board_event_types_board_id'), ['board_id'], unique=False)

    op.create_table('board_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('entry_id', sa.Integer(), nullable=False),
    sa.Column('board_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=64), nullable=False),
    sa.Column('at', sa.DateTime(), nullable=False),
    sa.Column('value', sa.Float(), nullable=False),
    sa.Column('unit', sa.String(length=16), nullable=True),
    sa.Column('confidence', sa.String(length=8), nullable=False),
    sa.Column('raw', sa.String(length=255), nullable=True),
    sa.ForeignKeyConstraint(['board_id'], ['boards.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['entry_id'], ['board_entries.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('board_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_board_events_at'), ['at'], unique=False)
        batch_op.create_index(batch_op.f('ix_board_events_board_id'), ['board_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_board_events_entry_id'), ['entry_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('board_events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_board_events_entry_id'))
        batch_op.drop_index(batch_op.f('ix_board_events_board_id'))
        batch_op.drop_index(batch_op.f('ix_board_events_at'))

    op.drop_table('board_events')
    with op.batch_alter_table('board_event_types', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_board_event_types_board_id'))

    op.drop_table('board_event_types')
