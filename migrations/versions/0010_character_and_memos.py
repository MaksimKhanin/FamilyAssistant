"""Характер ассистента и памятки по областям.

Две вещи, которые человек пишет о себе и об ассистенте свободным текстом.

Характер — колонка у участника, рядом с оформлением и самостоятельностью: он один
на человека и нужен всегда, отдельная таблица ради одной строки была бы лишней.

Памятки — таблица: их столько, сколько у человека областей, и завтра модулей
станет больше. Пара (участник, модуль) уникальна; пустую памятку код не хранит,
а удаляет строку, поэтому наличие строки означает «человек тут что-то написал».

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0010'
down_revision: Union[str, None] = '0009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    # База, развёрнутая `create_all()` без штампа головы, уже содержит и колонку,
    # и таблицу — довозить нечего.
    if "assistant_character" not in {c["name"] for c in inspector.get_columns("users")}:
        with op.batch_alter_table('users', schema=None) as batch_op:
            batch_op.add_column(sa.Column('assistant_character', sa.Text(), nullable=True))

    if "module_memos" not in inspector.get_table_names():
        op.create_table(
            'module_memos',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('module', sa.String(length=32), nullable=False),
            sa.Column('text', sa.Text(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('user_id', 'module', name='uq_module_memo'),
        )
        op.create_index(op.f('ix_module_memos_user_id'), 'module_memos', ['user_id'])


def downgrade() -> None:
    op.drop_index(op.f('ix_module_memos_user_id'), table_name='module_memos')
    op.drop_table('module_memos')
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('assistant_character')
