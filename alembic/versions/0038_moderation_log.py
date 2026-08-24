"""Automatic Moderation Activity Log -- `moderation_log_entries`.

Revision ID: 0038_moderation_log
Revises: 0037_admin_role_tiers
Create Date: 2026-08-24

Foundation table for Phases 4/5 (claim system, stats, the 3 views) --
see `ModerationLogEntry`'s own docstring (app/models/moderation_log.py)
for the full column-level reasoning.

Two new native Postgres ENUM types (`moderation_action`,
`moderation_target_type`) plus REUSE of the existing `admin_role` type
(migration 0037) via `create_type=False` for `actor_role` -- this is
deliberately the SAME closed set of values as `User.admin_role`, not a
parallel enum that could drift from it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0038_moderation_log"
down_revision: str | None = "0037_admin_role_tiers"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# Mirrors `app.models.enums.ModerationAction`/`ModerationTargetType`'s
# member VALUES exactly -- same local-literal-tuple convention as every
# other enum-backed migration in this codebase.
_MODERATION_ACTION_VALUES = (
    "video_override",
    "video_delete",
    "video_reject_and_purge",
    "chat_unban",
)
_MODERATION_TARGET_TYPE_VALUES = ("student_video", "user")


def upgrade() -> None:
    moderation_action_type = postgresql.ENUM(*_MODERATION_ACTION_VALUES, name="moderation_action")
    moderation_action_type.create(op.get_bind(), checkfirst=True)

    moderation_target_type_type = postgresql.ENUM(
        *_MODERATION_TARGET_TYPE_VALUES, name="moderation_target_type"
    )
    moderation_target_type_type.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "moderation_log_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "actor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "actor_role",
            postgresql.ENUM(*("moderator", "senior_moderator", "owner"), name="admin_role", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "action",
            postgresql.ENUM(*_MODERATION_ACTION_VALUES, name="moderation_action", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "target_type",
            postgresql.ENUM(*_MODERATION_TARGET_TYPE_VALUES, name="moderation_target_type", create_type=False),
            nullable=False,
        ),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("outcome", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_index(
        "ix_moderation_log_actor_id_created_at",
        "moderation_log_entries",
        ["actor_id", "created_at"],
    )
    op.create_index(
        "ix_moderation_log_created_at",
        "moderation_log_entries",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_moderation_log_created_at", table_name="moderation_log_entries")
    op.drop_index("ix_moderation_log_actor_id_created_at", table_name="moderation_log_entries")
    op.drop_table("moderation_log_entries")
    postgresql.ENUM(name="moderation_target_type").drop(op.get_bind(), checkfirst=True)
    postgresql.ENUM(name="moderation_action").drop(op.get_bind(), checkfirst=True)
