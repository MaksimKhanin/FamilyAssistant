"""«Подход» включается по умолчанию новым — существующим закрепляется opt-out.

Пара к ADR-0015 (docs/adr/0015-podhod-vklyuchyon-novym-po-umolchaniyu.md):
планировщик начинает читать включённость модуля с default=True, и чтобы это
не включило модуль молча никому из живущих, каждому существующему человеку
без явной строки кладётся `module_access(module='relationship', enabled=False)`.

Это та же операция, что в 0015, — повторённая, потому что между 0015 и этой
раскаткой могли завестись люди без строки: до сих пор отсутствие строки
означало «выключено» (default=False у планировщика), теперь означает
«включено». Новые люди, заведённые после этой миграции, получают модуль
включённым — тумблер в онбординге виден и выключается одним нажатием.

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-20

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0016'
down_revision: Union[str, None] = '0015'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MODULE = 'relationship'


def _tables():
    users = sa.table('users', sa.column('id', sa.Integer))
    module_access = sa.table(
        'module_access',
        sa.column('user_id', sa.Integer),
        sa.column('module', sa.String),
        sa.column('enabled', sa.Boolean),
    )
    return users, module_access


def upgrade() -> None:
    bind = op.get_bind()
    users, module_access = _tables()

    already = {
        row[0] for row in bind.execute(
            sa.select(module_access.c.user_id).where(module_access.c.module == MODULE)
        )
    }
    all_ids = [row[0] for row in bind.execute(sa.select(users.c.id))]
    missing = [user_id for user_id in all_ids if user_id not in already]

    if missing:
        bind.execute(
            module_access.insert(),
            [{'user_id': user_id, 'module': MODULE, 'enabled': False} for user_id in missing],
        )


def downgrade() -> None:
    # Строки 0015 и этой миграции неразличимы; откат намеренно ничего не
    # удаляет — снятие явного opt-out включило бы модуль людям молча.
    pass
