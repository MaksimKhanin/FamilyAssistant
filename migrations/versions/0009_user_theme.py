"""Оформление панели у каждого своё.

Тем две — «Ночное» и «Тёплое», — и выбирать между ними приходится не один раз
на семью: вечером за домом смотрит один человек, днём за столом сидит другой.
Поэтому колонка встаёт к участнику, а не к семье; акцент семьи
(`family_settings.accent_color`) остаётся общим и работает поверх обеих тем.

Дефолт — тёплое: панель до сих пор была светлой, и переезд не должен менять
вид ни у кого, пока человек сам не переключил.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0009'
down_revision: Union[str, None] = '0008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # База, развёрнутая `create_all()` без штампа головы, уже содержит колонки
    # из моделей — довозить нечего.
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("users")}
    if "theme" in columns:
        return

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('theme', sa.String(length=8), nullable=False,
                                      server_default='warm'))


def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('theme')
