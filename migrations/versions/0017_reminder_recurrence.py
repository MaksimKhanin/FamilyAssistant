"""Повторяющиеся напоминания: колонка recurrence (тикет #79, backlog #8).

NULL — разовое напоминание, как жило всегда; 'daily' / 'weekly' / 'monthly' —
сработав, напоминание переезжает на следующий раз (день недели и число
сохраняются от собственного remind_at), reminded_at у него не ставится.

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0017'
down_revision: Union[str, None] = '0016'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # База, развёрнутая `create_all()` без штампа головы, уже содержит колонки
    # из моделей — довозить нечего.
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("reminders")}
    if "recurrence" in columns:
        return

    op.add_column('reminders', sa.Column('recurrence', sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column('reminders', 'recurrence')
