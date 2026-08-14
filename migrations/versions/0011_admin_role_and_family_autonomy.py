"""Администратор вместо главы семьи, самостоятельность — на всю семью.

Три вещи одной миграцией, потому что это одно решение (ADR-0007): роль главы
семьи разделилась на администратора и участника, и всё, что глава настраивал
«себе», стало общесемейным.

  * `users.role`: «head» → «admin». Учётка, которая была главой, становится
    служебной — ассистента под ней больше нет, и человеку нужна вторая учётка
    участника. Сделать её он может сам, войдя администратором;
  * `users.autonomy` уезжает в `family_settings.autonomy`. Значение берём у
    первого администратора семьи (это и был тот, кто настраивал панель), а если
    админа нет — у самого раннего участника;
  * `tool_policies` переезжает с человека на семью. Прежние исключения не
    переносятся: их выставляли конкретному человеку под его самостоятельность,
    и «поднять» чужое правило на всю семью значило бы тихо раздать чужие права.
    Таблица пересоздаётся пустой — режимы задаются заново на экране
    «Агент и инструменты», и их там единицы.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0011'
down_revision: Union[str, None] = '0010'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    op.execute("UPDATE users SET role = 'admin' WHERE role = 'head'")

    family_columns = {c["name"] for c in inspector.get_columns("family_settings")}
    if "autonomy" not in family_columns:
        with op.batch_alter_table('family_settings', schema=None) as batch_op:
            batch_op.add_column(sa.Column('autonomy', sa.Integer(), nullable=False,
                                          server_default='1'))

    # Значение переносим только с базы, которая его ещё хранит: на свежей
    # (развёрнутой `create_all()`) колонки у пользователя уже нет.
    user_columns = {c["name"] for c in inspector.get_columns("users")}
    if "autonomy" in user_columns:
        op.execute("""
            UPDATE family_settings
               SET autonomy = COALESCE((
                   SELECT u.autonomy FROM users u
                    WHERE u.family_id = family_settings.family_id
                    ORDER BY CASE WHEN u.role = 'admin' THEN 0 ELSE 1 END, u.id
                    LIMIT 1
               ), 1)
        """)
        with op.batch_alter_table('users', schema=None) as batch_op:
            batch_op.drop_column('autonomy')

    policy_columns = {c["name"] for c in inspector.get_columns("tool_policies")}
    if "family_id" not in policy_columns:
        op.drop_table('tool_policies')
        op.create_table(
            'tool_policies',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('family_id', sa.Integer(), nullable=False),
            sa.Column('tool', sa.String(length=64), nullable=False),
            sa.Column('mode', sa.String(length=8), nullable=False),
            sa.ForeignKeyConstraint(['family_id'], ['families.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('family_id', 'tool', name='uq_tool_policy'),
        )
        op.create_index(op.f('ix_tool_policies_family_id'), 'tool_policies', ['family_id'])


def downgrade() -> None:
    op.drop_index(op.f('ix_tool_policies_family_id'), table_name='tool_policies')
    op.drop_table('tool_policies')
    op.create_table(
        'tool_policies',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('tool', sa.String(length=64), nullable=False),
        sa.Column('mode', sa.String(length=8), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'tool', name='uq_tool_policy'),
    )
    op.create_index(op.f('ix_tool_policies_user_id'), 'tool_policies', ['user_id'])

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('autonomy', sa.Integer(), nullable=False,
                                      server_default='1'))
    with op.batch_alter_table('family_settings', schema=None) as batch_op:
        batch_op.drop_column('autonomy')

    op.execute("UPDATE users SET role = 'head' WHERE role = 'admin'")
