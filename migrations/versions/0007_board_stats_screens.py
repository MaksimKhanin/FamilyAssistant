"""Табло — экран одного показателя по ряду задачи статистики (тикет #32, спека #19).

Каскад от задачи: табло только показывает её ряд и не переживает его. Каскад от
участника: пункт навигации личный, и с уходом человека уносится вместе с ним.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-13

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0007'
down_revision: Union[str, Sequence[str], None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # База, развёрнутая `create_all()` без штампа головы, уже содержит таблицы
    # из моделей — довозить нечего.
    if "board_stats_screens" in sa.inspect(op.get_bind()).get_table_names():
        return

    op.create_table('board_stats_screens',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('task_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('form', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['task_id'], ['board_stats_tasks.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('board_stats_screens', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_board_stats_screens_task_id'), ['task_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_board_stats_screens_user_id'), ['user_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('board_stats_screens', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_board_stats_screens_user_id'))
        batch_op.drop_index(batch_op.f('ix_board_stats_screens_task_id'))

    op.drop_table('board_stats_screens')
