"""Схема знаний: разделы → доски → записи, плюс поимённый доступ (спека #19).

`author_id` записи — SET NULL: запись принадлежит документу и переживает
своего автора (ADR-0004). Всё остальное каскадится сверху вниз.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # `create_all()` при старте сервера мог успеть создать эти таблицы из
    # моделей — тогда база уже в целевом состоянии и делать нечего.
    if "sections" in sa.inspect(op.get_bind()).get_table_names():
        return

    op.create_table('sections',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('pinned', sa.Boolean(), nullable=False),
    sa.Column('last_activity_at', sa.DateTime(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('sections', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sections_user_id'), ['user_id'], unique=False)

    op.create_table('boards',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('section_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('instruction', sa.Text(), nullable=True),
    sa.Column('last_activity_at', sa.DateTime(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['section_id'], ['sections.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('boards', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_boards_section_id'), ['section_id'], unique=False)

    op.create_table('board_entries',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('board_id', sa.Integer(), nullable=False),
    sa.Column('author_id', sa.Integer(), nullable=True),
    sa.Column('by_assistant', sa.Boolean(), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('edited_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['author_id'], ['users.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['board_id'], ['boards.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('board_entries', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_board_entries_author_id'), ['author_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_board_entries_board_id'), ['board_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_board_entries_created_at'), ['created_at'], unique=False)

    op.create_table('board_shares',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('board_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('right', sa.String(length=8), nullable=False),
    sa.ForeignKeyConstraint(['board_id'], ['boards.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('board_id', 'user_id', name='uq_board_share')
    )
    with op.batch_alter_table('board_shares', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_board_shares_board_id'), ['board_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_board_shares_user_id'), ['user_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('board_shares', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_board_shares_user_id'))
        batch_op.drop_index(batch_op.f('ix_board_shares_board_id'))

    op.drop_table('board_shares')
    with op.batch_alter_table('board_entries', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_board_entries_created_at'))
        batch_op.drop_index(batch_op.f('ix_board_entries_board_id'))
        batch_op.drop_index(batch_op.f('ix_board_entries_author_id'))

    op.drop_table('board_entries')
    with op.batch_alter_table('boards', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_boards_section_id'))

    op.drop_table('boards')
    with op.batch_alter_table('sections', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sections_user_id'))

    op.drop_table('sections')
