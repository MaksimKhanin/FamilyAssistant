"""Напоминания: своя таблица вне знаний (тикет #24, спека #19).

Каскад от участника; на таблицы знаний ничего не ссылается — напоминание
не раздел, не доска и не запись.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # База, развёрнутая `create_all()` без штампа головы (установки до появления
    # Alembic, обновившиеся сразу на этот код), уже содержит таблицу из моделей.
    if "reminders" in sa.inspect(op.get_bind()).get_table_names():
        return

    op.create_table('reminders',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('remind_at', sa.DateTime(), nullable=False),
    sa.Column('reminded_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('reminders', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_reminders_remind_at'), ['remind_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_reminders_user_id'), ['user_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('reminders', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_reminders_user_id'))
        batch_op.drop_index(batch_op.f('ix_reminders_remind_at'))

    op.drop_table('reminders')
