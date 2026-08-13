"""Задачи статистики по доске и ряд значений по дням (тикет #31, спека #19).

Задача каскадится от доски — показатель не переживает лог, по которому считался,
— и от автора: чужую задачу в своей сводке никто не увидит. Точка ряда каскадится
от задачи: витрина не переживает того, что показывает.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # База, развёрнутая `create_all()` без штампа головы, уже содержит таблицы
    # из моделей — довозить нечего.
    if "board_stats_tasks" in sa.inspect(op.get_bind()).get_table_names():
        return

    op.create_table('board_stats_tasks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('board_id', sa.Integer(), nullable=False),
    sa.Column('author_id', sa.Integer(), nullable=False),
    sa.Column('request', sa.Text(), nullable=False),
    sa.Column('kind', sa.String(length=64), nullable=False),
    sa.Column('digest_kind', sa.String(length=32), nullable=False),
    sa.Column('share_all', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['author_id'], ['users.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['board_id'], ['boards.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('board_stats_tasks', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_board_stats_tasks_author_id'), ['author_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_board_stats_tasks_board_id'), ['board_id'], unique=False)

    op.create_table('board_stats_points',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.Column('day', sa.Date(), nullable=False),
    sa.Column('value', sa.Float(), nullable=False),
    sa.Column('unit', sa.String(length=16), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['task_id'], ['board_stats_tasks.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('task_id', 'day', name='uq_board_stats_point')
    )
    with op.batch_alter_table('board_stats_points', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_board_stats_points_day'), ['day'], unique=False)
        batch_op.create_index(batch_op.f('ix_board_stats_points_task_id'), ['task_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('board_stats_points', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_board_stats_points_task_id'))
        batch_op.drop_index(batch_op.f('ix_board_stats_points_day'))

    op.drop_table('board_stats_points')
    with op.batch_alter_table('board_stats_tasks', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_board_stats_tasks_board_id'))
        batch_op.drop_index(batch_op.f('ix_board_stats_tasks_author_id'))

    op.drop_table('board_stats_tasks')
