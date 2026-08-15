"""Идеи блюд и пожелания к рациону.

Подбор питания перестал жить один переход: то, что ассистент предложил, теперь
лежит в `meal_ideas` — иначе отмечать в нём нечего, а отметить понравившееся и
есть смысл экрана «План питания» (ADR-0010). Едой эти строки не становятся:
`meals` — про съеденное, а тут только идея, и в баланс дня она не идёт.

`nutrition_profiles.preferences` — пожелания к рациону, написанные человеком:
что он ест, чего не ест, что любит. Рядом с целью и нормой, потому что это про
одного человека и нужно ровно там же, где они.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-15

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0012'
down_revision: Union[str, None] = '0011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    # База, поднятая `create_all()` из моделей, уже в целевом состоянии —
    # довозить нечего (так же, как в 0010).
    if "preferences" not in {c["name"] for c in inspector.get_columns("nutrition_profiles")}:
        with op.batch_alter_table('nutrition_profiles', schema=None) as batch_op:
            batch_op.add_column(sa.Column('preferences', sa.Text(), nullable=True))

    if "meal_ideas" not in inspector.get_table_names():
        op.create_table(
            'meal_ideas',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('title', sa.String(length=128), nullable=False),
            sa.Column('slot', sa.String(length=16), nullable=True),
            sa.Column('kcal', sa.Integer(), nullable=False),
            sa.Column('day_title', sa.String(length=32), nullable=True),
            sa.Column('position', sa.Integer(), nullable=False),
            sa.Column('saved', sa.Boolean(), nullable=False),
            sa.Column('recipe', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index(op.f('ix_meal_ideas_user_id'), 'meal_ideas', ['user_id'])


def downgrade() -> None:
    op.drop_index(op.f('ix_meal_ideas_user_id'), table_name='meal_ideas')
    op.drop_table('meal_ideas')
    with op.batch_alter_table('nutrition_profiles', schema=None) as batch_op:
        batch_op.drop_column('preferences')
