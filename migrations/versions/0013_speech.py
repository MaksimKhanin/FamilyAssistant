"""Озвучка: чем читать вслух — на семью, читать ли — каждому своё.

Колонки встают в две таблицы, и это не удобство, а само устройство настройки
(app/core/speech.py):

  * `family_settings.speech_mode` / `speech_voice` / `speech_rate` — выбор того
    же рода, что ядро и зрение: голосом устройства или моделью озвучки, каким
    голосом и с какой скоростью. Это про то, уходит ли текст ответа из дома и
    кто за него платит, — решение администратора на весь дом;
  * `users.speech_enabled` — читать ли вслух этому человеку. Личное, как
    оформление, и выключенное по умолчанию: заговорить без спроса панель не
    должна.

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
    inspector = sa.inspect(op.get_bind())

    # База, поднятая `create_all()` из моделей, уже в целевом состоянии —
    # довозить нечего (так же, как в 0012).
    family_columns = {c["name"] for c in inspector.get_columns("family_settings")}
    if "speech_mode" not in family_columns:
        with op.batch_alter_table('family_settings', schema=None) as batch_op:
            batch_op.add_column(sa.Column('speech_mode', sa.String(length=16), nullable=False,
                                          server_default='device'))
            batch_op.add_column(sa.Column('speech_voice', sa.String(length=32), nullable=False,
                                          server_default='alloy'))
            batch_op.add_column(sa.Column('speech_rate', sa.Integer(), nullable=False,
                                          server_default='100'))

    user_columns = {c["name"] for c in inspector.get_columns("users")}
    if "speech_enabled" not in user_columns:
        with op.batch_alter_table('users', schema=None) as batch_op:
            batch_op.add_column(sa.Column('speech_enabled', sa.Boolean(), nullable=False,
                                          server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('speech_enabled')
    with op.batch_alter_table('family_settings', schema=None) as batch_op:
        batch_op.drop_column('speech_rate')
        batch_op.drop_column('speech_voice')
        batch_op.drop_column('speech_mode')
