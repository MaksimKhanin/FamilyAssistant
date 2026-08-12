"""Флаг «всем» и право по умолчанию — на доске (тикет #28, спека #19).

Поимённый доступ живёт в board_shares; «всем» — живое правило на самой доске,
чтобы новый человек в семье получал такую доску сам, без повторного действия
владельца.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # База, развёрнутая `create_all()` без штампа головы, уже содержит колонки
    # из моделей — довозить нечего.
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("boards")}
    if "share_all" in columns:
        return

    with op.batch_alter_table('boards', schema=None) as batch_op:
        batch_op.add_column(sa.Column('share_all', sa.Boolean(), nullable=False,
                                      server_default=sa.false()))
        batch_op.add_column(sa.Column('share_all_right', sa.String(length=8), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('boards', schema=None) as batch_op:
        batch_op.drop_column('share_all_right')
        batch_op.drop_column('share_all')
