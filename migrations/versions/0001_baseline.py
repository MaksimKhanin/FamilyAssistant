"""Baseline: существующая схема, какой её создавал `create_all()`.

На пустой базе создаёт всё с нуля; на живой базе не делает ничего — она уже
в этом состоянии, миграция лишь фиксирует его за Alembic как точку отсчёта.

Revision ID: 0001
Revises:
Create Date: 2026-08-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Живая база уже создана `create_all()` и совпадает с этой схемой по
    # построению: DDL ниже сгенерирован из тех же моделей. «users» — свидетель:
    # таблица есть в любой непустой базе проекта с первого дня.
    if "users" in sa.inspect(op.get_bind()).get_table_names():
        return

    op.create_table('families',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('cameras',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('family_id', sa.Integer(), nullable=False),
    sa.Column('slug', sa.String(length=64), nullable=False),
    sa.Column('label', sa.String(length=64), nullable=False),
    sa.Column('zone', sa.String(length=32), nullable=False),
    sa.Column('notify_enabled', sa.Boolean(), nullable=False),
    sa.Column('quiet_from', sa.Integer(), nullable=False),
    sa.Column('quiet_to', sa.Integer(), nullable=False),
    sa.Column('always_notify', sa.Boolean(), nullable=False),
    sa.Column('retention_days', sa.Integer(), nullable=False),
    sa.Column('hint', sa.String(length=255), nullable=True),
    sa.Column('last_seen_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['family_id'], ['families.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('family_id', 'slug', name='uq_camera_slug')
    )
    with op.batch_alter_table('cameras', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_cameras_family_id'), ['family_id'], unique=False)

    op.create_table('family_settings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('family_id', sa.Integer(), nullable=False),
    sa.Column('core_model', sa.String(length=16), nullable=False),
    sa.Column('vlm_mode', sa.String(length=16), nullable=False),
    sa.Column('yolo_model', sa.String(length=16), nullable=False),
    sa.Column('frames_stay_home', sa.Boolean(), nullable=False),
    sa.Column('cloud_budget_eur', sa.Integer(), nullable=False),
    sa.Column('cloud_spent_eur', sa.Float(), nullable=False),
    sa.Column('rag_sources_json', sa.Text(), nullable=False),
    sa.Column('accent_color', sa.String(length=16), nullable=False),
    sa.ForeignKeyConstraint(['family_id'], ['families.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('family_id')
    )
    op.create_table('trace_settings',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('family_id', sa.Integer(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('keep_runs', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['family_id'], ['families.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('family_id')
    )
    op.create_table('users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('family_id', sa.Integer(), nullable=False),
    sa.Column('username', sa.String(length=64), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=True),
    sa.Column('display_name', sa.String(length=64), nullable=False),
    sa.Column('relation', sa.String(length=32), nullable=True),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('invite_code', sa.String(length=32), nullable=True),
    sa.Column('telegram_id', sa.String(length=32), nullable=True),
    sa.Column('avatar_slot', sa.Integer(), nullable=False),
    sa.Column('autonomy', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['family_id'], ['families.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_users_family_id'), ['family_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_users_invite_code'), ['invite_code'], unique=False)
        batch_op.create_index(batch_op.f('ix_users_telegram_id'), ['telegram_id'], unique=True)
        batch_op.create_index(batch_op.f('ix_users_username'), ['username'], unique=True)

    op.create_table('action_log',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('tool', sa.String(length=64), nullable=False),
    sa.Column('arguments_json', sa.Text(), nullable=False),
    sa.Column('outcome', sa.String(length=16), nullable=False),
    sa.Column('mode', sa.String(length=16), nullable=False),
    sa.Column('summary', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('action_log', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_action_log_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_action_log_user_id'), ['user_id'], unique=False)

    op.create_table('activity_log',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('happened_at', sa.DateTime(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('value', sa.Float(), nullable=False),
    sa.Column('kcal', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('activity_log', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_activity_log_happened_at'), ['happened_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_activity_log_user_id'), ['user_id'], unique=False)

    op.create_table('agent_runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('session_id', sa.String(length=32), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('subject_id', sa.Integer(), nullable=True),
    sa.Column('channel', sa.String(length=16), nullable=False),
    sa.Column('trigger', sa.Text(), nullable=True),
    sa.Column('reply', sa.Text(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('prompt_tokens', sa.Integer(), nullable=False),
    sa.Column('completion_tokens', sa.Integer(), nullable=False),
    sa.Column('total_tokens', sa.Integer(), nullable=False),
    sa.Column('llm_calls', sa.Integer(), nullable=False),
    sa.Column('tool_calls', sa.Integer(), nullable=False),
    sa.Column('duration_ms', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('agent_runs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_agent_runs_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_agent_runs_session_id'), ['session_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_agent_runs_user_id'), ['user_id'], unique=False)

    op.create_table('chat_messages',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('channel', sa.String(length=16), nullable=False),
    sa.Column('payload_json', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('chat_messages', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_chat_messages_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_chat_messages_user_id'), ['user_id'], unique=False)

    op.create_table('connectors',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('service', sa.String(length=32), nullable=False),
    sa.Column('connected', sa.Boolean(), nullable=False),
    sa.Column('permission', sa.String(length=8), nullable=False),
    sa.Column('credentials_json', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'service', name='uq_connector')
    )
    with op.batch_alter_table('connectors', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_connectors_user_id'), ['user_id'], unique=False)

    op.create_table('meals',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('eaten_at', sa.DateTime(), nullable=False),
    sa.Column('source', sa.String(length=8), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('title', sa.String(length=128), nullable=False),
    sa.Column('kcal', sa.Integer(), nullable=False),
    sa.Column('protein', sa.Integer(), nullable=False),
    sa.Column('fat', sa.Integer(), nullable=False),
    sa.Column('carbs', sa.Integer(), nullable=False),
    sa.Column('portion', sa.String(length=128), nullable=True),
    sa.Column('confidence', sa.String(length=8), nullable=True),
    sa.Column('raw_input', sa.Text(), nullable=True),
    sa.Column('image_path', sa.String(length=512), nullable=True),
    sa.Column('note', sa.String(length=255), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('meals', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_meals_eaten_at'), ['eaten_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_meals_user_id'), ['user_id'], unique=False)

    op.create_table('module_access',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('module', sa.String(length=32), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'module', name='uq_module_access')
    )
    with op.batch_alter_table('module_access', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_module_access_user_id'), ['user_id'], unique=False)

    op.create_table('notes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('source', sa.String(length=64), nullable=False),
    sa.Column('pinned', sa.Boolean(), nullable=False),
    sa.Column('when_text', sa.String(length=128), nullable=True),
    sa.Column('remind_at', sa.DateTime(), nullable=True),
    sa.Column('reminded_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('notes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_notes_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_notes_remind_at'), ['remind_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_notes_user_id'), ['user_id'], unique=False)

    op.create_table('nutrition_profiles',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('daily_kcal', sa.Integer(), nullable=False),
    sa.Column('goal', sa.String(length=8), nullable=False),
    sa.Column('height_cm', sa.Integer(), nullable=True),
    sa.Column('weight_kg', sa.Float(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('nutrition_profiles', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_nutrition_profiles_user_id'), ['user_id'], unique=True)

    op.create_table('pending_actions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('tool', sa.String(length=64), nullable=False),
    sa.Column('arguments_json', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('attachment_path', sa.String(length=512), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('resolved_at', sa.DateTime(), nullable=True),
    sa.Column('result_summary', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('pending_actions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_pending_actions_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_pending_actions_user_id'), ['user_id'], unique=False)

    op.create_table('push_subscriptions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('endpoint', sa.String(length=512), nullable=False),
    sa.Column('p256dh', sa.String(length=255), nullable=False),
    sa.Column('auth', sa.String(length=64), nullable=False),
    sa.Column('device_label', sa.String(length=128), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('last_used_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('endpoint')
    )
    with op.batch_alter_table('push_subscriptions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_push_subscriptions_user_id'), ['user_id'], unique=False)

    op.create_table('scheduled_jobs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('at_time', sa.String(length=5), nullable=False),
    sa.Column('weekday', sa.Integer(), nullable=True),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('last_run_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'kind', name='uq_scheduled_job')
    )
    with op.batch_alter_table('scheduled_jobs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_scheduled_jobs_user_id'), ['user_id'], unique=False)

    op.create_table('security_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('family_id', sa.Integer(), nullable=False),
    sa.Column('camera_id', sa.Integer(), nullable=False),
    sa.Column('happened_at', sa.DateTime(), nullable=False),
    sa.Column('verdict', sa.String(length=16), nullable=False),
    sa.Column('reason', sa.String(length=255), nullable=True),
    sa.Column('detected_class', sa.String(length=32), nullable=True),
    sa.Column('confidence', sa.Float(), nullable=True),
    sa.Column('area', sa.Integer(), nullable=True),
    sa.Column('snapshot_path', sa.String(length=512), nullable=True),
    sa.Column('clip_path', sa.String(length=512), nullable=True),
    sa.Column('notified_at', sa.DateTime(), nullable=True),
    sa.Column('resolution', sa.String(length=16), nullable=True),
    sa.Column('resolved_at', sa.DateTime(), nullable=True),
    sa.Column('classified_by', sa.String(length=16), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['camera_id'], ['cameras.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['family_id'], ['families.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('security_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_security_events_camera_id'), ['camera_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_security_events_family_id'), ['family_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_security_events_happened_at'), ['happened_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_security_events_verdict'), ['verdict'], unique=False)

    op.create_table('tool_policies',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('tool', sa.String(length=64), nullable=False),
    sa.Column('mode', sa.String(length=8), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'tool', name='uq_tool_policy')
    )
    with op.batch_alter_table('tool_policies', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_tool_policies_user_id'), ['user_id'], unique=False)

    op.create_table('agent_trace_steps',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('run_id', sa.Integer(), nullable=False),
    sa.Column('step_no', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=8), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('request_json', sa.Text(), nullable=True),
    sa.Column('response_json', sa.Text(), nullable=True),
    sa.Column('prompt_tokens', sa.Integer(), nullable=False),
    sa.Column('completion_tokens', sa.Integer(), nullable=False),
    sa.Column('total_tokens', sa.Integer(), nullable=False),
    sa.Column('duration_ms', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['agent_runs.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('agent_trace_steps', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_agent_trace_steps_run_id'), ['run_id'], unique=False)

    op.create_table('security_media',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('family_id', sa.Integer(), nullable=False),
    sa.Column('camera_id', sa.Integer(), nullable=False),
    sa.Column('event_id', sa.Integer(), nullable=True),
    sa.Column('filename', sa.String(length=255), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('rel_path', sa.String(length=512), nullable=False),
    sa.Column('thumb_rel_path', sa.String(length=512), nullable=True),
    sa.Column('captured_at', sa.DateTime(), nullable=False),
    sa.Column('stored_at', sa.DateTime(), nullable=False),
    sa.Column('size_bytes', sa.Integer(), nullable=True),
    sa.Column('is_alert', sa.Boolean(), nullable=False),
    sa.Column('is_merged', sa.Boolean(), nullable=False),
    sa.Column('detected_class', sa.String(length=32), nullable=True),
    sa.Column('confidence', sa.Float(), nullable=True),
    sa.Column('area', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['camera_id'], ['cameras.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['event_id'], ['security_events.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['family_id'], ['families.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('camera_id', 'filename', name='uq_media_camera_filename')
    )
    with op.batch_alter_table('security_media', schema=None) as batch_op:
        batch_op.create_index('ix_media_camera_captured', ['camera_id', 'captured_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_security_media_camera_id'), ['camera_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_security_media_captured_at'), ['captured_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_security_media_family_id'), ['family_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_security_media_is_alert'), ['is_alert'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('security_media', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_security_media_is_alert'))
        batch_op.drop_index(batch_op.f('ix_security_media_family_id'))
        batch_op.drop_index(batch_op.f('ix_security_media_captured_at'))
        batch_op.drop_index(batch_op.f('ix_security_media_camera_id'))
        batch_op.drop_index('ix_media_camera_captured')

    op.drop_table('security_media')
    with op.batch_alter_table('agent_trace_steps', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_agent_trace_steps_run_id'))

    op.drop_table('agent_trace_steps')
    with op.batch_alter_table('tool_policies', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_tool_policies_user_id'))

    op.drop_table('tool_policies')
    with op.batch_alter_table('security_events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_security_events_verdict'))
        batch_op.drop_index(batch_op.f('ix_security_events_happened_at'))
        batch_op.drop_index(batch_op.f('ix_security_events_family_id'))
        batch_op.drop_index(batch_op.f('ix_security_events_camera_id'))

    op.drop_table('security_events')
    with op.batch_alter_table('scheduled_jobs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_scheduled_jobs_user_id'))

    op.drop_table('scheduled_jobs')
    with op.batch_alter_table('push_subscriptions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_push_subscriptions_user_id'))

    op.drop_table('push_subscriptions')
    with op.batch_alter_table('pending_actions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_pending_actions_user_id'))
        batch_op.drop_index(batch_op.f('ix_pending_actions_created_at'))

    op.drop_table('pending_actions')
    with op.batch_alter_table('nutrition_profiles', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_nutrition_profiles_user_id'))

    op.drop_table('nutrition_profiles')
    with op.batch_alter_table('notes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_notes_user_id'))
        batch_op.drop_index(batch_op.f('ix_notes_remind_at'))
        batch_op.drop_index(batch_op.f('ix_notes_created_at'))

    op.drop_table('notes')
    with op.batch_alter_table('module_access', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_module_access_user_id'))

    op.drop_table('module_access')
    with op.batch_alter_table('meals', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_meals_user_id'))
        batch_op.drop_index(batch_op.f('ix_meals_eaten_at'))

    op.drop_table('meals')
    with op.batch_alter_table('connectors', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_connectors_user_id'))

    op.drop_table('connectors')
    with op.batch_alter_table('chat_messages', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_chat_messages_user_id'))
        batch_op.drop_index(batch_op.f('ix_chat_messages_created_at'))

    op.drop_table('chat_messages')
    with op.batch_alter_table('agent_runs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_agent_runs_user_id'))
        batch_op.drop_index(batch_op.f('ix_agent_runs_session_id'))
        batch_op.drop_index(batch_op.f('ix_agent_runs_created_at'))

    op.drop_table('agent_runs')
    with op.batch_alter_table('activity_log', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_activity_log_user_id'))
        batch_op.drop_index(batch_op.f('ix_activity_log_happened_at'))

    op.drop_table('activity_log')
    with op.batch_alter_table('action_log', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_action_log_user_id'))
        batch_op.drop_index(batch_op.f('ix_action_log_created_at'))

    op.drop_table('action_log')
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_users_username'))
        batch_op.drop_index(batch_op.f('ix_users_telegram_id'))
        batch_op.drop_index(batch_op.f('ix_users_invite_code'))
        batch_op.drop_index(batch_op.f('ix_users_family_id'))

    op.drop_table('users')
    op.drop_table('trace_settings')
    op.drop_table('family_settings')
    with op.batch_alter_table('cameras', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_cameras_family_id'))

    op.drop_table('cameras')
    op.drop_table('families')
