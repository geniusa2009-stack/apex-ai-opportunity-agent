"""Review of Guild Content -- `guild_content_reports` /
`guild_content_report_entries`, plus new `ModerationAction`/
`ModerationTargetType` enum members.

Revision ID: 0047_guild_content_reports
Revises: 0046_content_blacklist
Create Date: 2026-09-01

See `app/models/guild_moderation.py`'s own module docstring for the full
Step 1/2 investigation: guild content already has two real, automatic
pre-publish checks (Gemini safety/PII + the general blacklist, the
latter already visible via the existing Blacklist tab) -- the one
genuine gap was that no user could report an already-published post or
comment. `reason` reuses the EXISTING `chat_report_reason` native enum
(created by migration 0040) unchanged -- no new enum type needed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0047_guild_content_reports"
down_revision: str | None = "0046_content_blacklist"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_NEW_MODERATION_ACTIONS = [
    "guild_report_dismiss",
    "guild_content_delete",
]
_NEW_TARGET_TYPES = ["guild_content_report"]


def upgrade() -> None:
    for value in _NEW_MODERATION_ACTIONS:
        op.execute(f"ALTER TYPE moderation_action ADD VALUE IF NOT EXISTS '{value}'")
    for value in _NEW_TARGET_TYPES:
        op.execute(f"ALTER TYPE moderation_target_type ADD VALUE IF NOT EXISTS '{value}'")

    chat_report_reason = postgresql.ENUM(
        "spam", "harassment", "inappropriate_content", "other",
        name="chat_report_reason",
    )
    # Already created by migration 0040 -- `checkfirst=True` makes this a
    # real no-op here, not a duplicate-type error. Referenced (not
    # re-created) below since `GuildContentReportEntry.reason` reuses the
    # SAME real enum type `ChatMessageReportEntry.reason` already uses.
    chat_report_reason.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "guild_content_reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("target_type", sa.String(20), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
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
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_guild_content_reports_target", "guild_content_reports", ["target_type", "target_id"])
    op.create_index(
        "ix_guild_content_reports_status_created_at", "guild_content_reports", ["status", "created_at"]
    )
    op.create_index("ix_guild_content_reports_status", "guild_content_reports", ["status"])

    op.create_table(
        "guild_content_report_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "report_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("guild_content_reports.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "reporter_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reason", chat_report_reason, nullable=False),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("report_id", "reporter_id", name="uq_guild_report_entry_report_reporter"),
    )
    op.create_index(
        "ix_guild_content_report_entries_report_id", "guild_content_report_entries", ["report_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_guild_content_report_entries_report_id", table_name="guild_content_report_entries")
    op.drop_table("guild_content_report_entries")

    op.drop_index("ix_guild_content_reports_status", table_name="guild_content_reports")
    op.drop_index("ix_guild_content_reports_status_created_at", table_name="guild_content_reports")
    op.drop_index("ix_guild_content_reports_target", table_name="guild_content_reports")
    op.drop_table("guild_content_reports")

    # DISCLOSED: same "new enum VALUES are never removed on downgrade"
    # posture every prior migration in this chain already accepts --
    # Postgres has no `ALTER TYPE ... DROP VALUE`. `chat_report_reason`
    # itself is NOT dropped here either (unlike migration 0040's own
    # downgrade) -- it is still owned/used by `chat_message_report_entries`,
    # which this migration did not create and must not tear down.
