"""Модуль «Подход» — новых пользователей это не касается молча.

Не меняет схему — только данные. `is_module_enabled` (app/core/access.py)
по умолчанию `True`, когда строки `module_access` для этого человека ещё
нет вовсе, — годится для питания и безопасности, но не для этого модуля:
интимный профиль общения не должен активироваться никому, кто никогда его
не просил, просто оттого что модуль попал в ENABLED_MODULES.

Кладём каждому уже существующему человеку явную строку
`module_access(module='relationship', enabled=False)`. Тогда общий default
для этого модуля не срабатывает нигде — ни у `available_tools`, ни у
`enabled_modules`, ни у планировщика: строка уже есть и явно говорит
«выключено». Новые люди, заведённые после раскатки, увидят обычный тумблер
в матрице онбординга, как у любого togglable-модуля, — их эта миграция не
касается и не должна.

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0015'
down_revision: Union[str, None] = '0014'
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
    bind = op.get_bind()
    _, module_access = _tables()
    bind.execute(module_access.delete().where(module_access.c.module == MODULE))
