"""Transparency: Notify Users of Moderation Actions + Real Appeal Path --
new `moderation_appeals` table, plus two new `ModerationAction` enum
values (`appeal_upheld`, `appeal_denied`) and one new
`ModerationTargetType` enum value (`moderation_appeal`).

Revision ID: 0042_moderation_appeals
Revises: 0041_repeat_offender_detection
Create Date: 2026-08-25

See `app/models/moderation_appeal.py`'s own module docstring for the full
"what is a case" / claim-reuse / FK-vs-polymorphic-column reasoning, and
`app/services/moderation_appeal_service.py`'s own module docstring for
the real reversible-vs-acknowledgment-only design these two new
`ModerationAction` values support.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0042_moderation_appeals"
down_revision: str | None = "0041_repeat_offender_detection"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE moderation_action ADD VALUE IF NOT EXISTS 'appeal_upheld'")
    op.execute("ALTER TYPE moderation_action ADD VALUE IF NOT EXISTS 'appeal_denied'")
    op.execute("ALTER TYPE moderation_target_type ADD VALUE IF NOT EXISTS 'moderation_appeal'")

    op.create_table(
        "moderation_appeals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "log_entry_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("moderation_log_entries.id"),
            nullable=False,
        ),
        sa.Column(
            "student_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("additional_context", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column(
            "claimed_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "resolved_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_notes", sa.Text(), nullable=True),
        sa.Column("can_be_restored", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_moderation_appeals_log_entry_id", "moderation_appeals", ["log_entry_id"])
    op.create_index("ix_moderation_appeals_status_created_at", "moderation_appeals", ["status", "created_at"])
    op.create_index("ix_moderation_appeals_status", "moderation_appeals", ["status"])


def downgrade() -> None:
    # DISCLOSED: same "new enum value can't be un-added" posture as every
    # prior migration in this chain (0040/0041's own downgrades) -- only
    # the real, cleanly-reversible table/index creation is undone.
    op.drop_index("ix_moderation_appeals_status", table_name="moderation_appeals")
    op.drop_index("ix_moderation_appeals_status_created_at", table_name="moderation_appeals")
    op.drop_index("ix_moderation_appeals_log_entry_id", table_name="moderation_appeals")
    op.drop_table("moderation_appeals")
