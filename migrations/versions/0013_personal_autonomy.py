"""Личная самостоятельность и личные режимы инструментов.

Обе ручки, которые ADR-0008 унёс от человека к семье, получают личный слой
поверх семейного (ADR-0012). Семейные настройки остаются на месте и работают
как раньше — они умолчание дома:

  * `users.autonomy` возвращается, но уже как NULL-по-умолчанию: NULL значит
    «своей настройки нет, иду за домом». Прежняя колонка, снесённая миграцией
    0011, была NOT NULL со значением 1 — здесь она заводится пустой, потому что
    «человек ничего не выбирал» и «человек выбрал единицу» — разные вещи;
  * `user_tool_policies` — новая таблица, личные исключения по инструментам.
    Заводится пустой: переносить в неё семейные строки нельзя — это ровно то,
    что 0011 отказалась делать в обратную сторону.

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0013'
down_revision: Union[str, None] = '0012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "autonomy" not in {c["name"] for c in inspector.get_columns("users")}:
        with op.batch_alter_table('users', schema=None) as batch_op:
            batch_op.add_column(sa.Column('autonomy', sa.Integer(), nullable=True))

    if "user_tool_policies" not in inspector.get_table_names():
        op.create_table(
            'user_tool_policies',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('tool', sa.String(length=64), nullable=False),
            sa.Column('mode', sa.String(length=8), nullable=False),
            sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('user_id', 'tool', name='uq_user_tool_policy'),
        )
        op.create_index(op.f('ix_user_tool_policies_user_id'), 'user_tool_policies', ['user_id'])


def downgrade() -> None:
    op.drop_index(op.f('ix_user_tool_policies_user_id'), table_name='user_tool_policies')
    op.drop_table('user_tool_policies')
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('autonomy')
