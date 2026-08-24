"""Extend Moderation to Chat Messages -- `chat_message_reports` /
`chat_message_report_entries`, plus new `ModerationAction`/
`ModerationTargetType` enum members.

Revision ID: 0040_chat_message_reports
Revises: 0039_moderation_claims
Create Date: 2026-08-24

Builds on the confirmed-real claim system (0039). See
`app/models/chat_moderation.py`'s own module docstring for the full
"what is a case, this time" reasoning: the case is `ChatMessageReport`
(claimable via the SAME `claimed_by`/`claimed_at` pattern `StudentVideo`
already established), with individual report submissions living in a
separate `chat_message_report_entries` child table so one message can be
reported by multiple distinct users without duplicating queue rows.

`ModerationAction`/`ModerationTargetType` are real native Postgres ENUM
types (`moderation_action`/`moderation_target_type`, created by migration
0038 -- see `ModerationLogEntry`'s own column definitions). Postgres has
no `ALTER TYPE ... ADD VALUE IF NOT EXISTS` on older versions and no
single-statement "add several values" form, so each new member gets its
own `ALTER TYPE` statement. `ALTER TYPE ... ADD VALUE` cannot run inside
the same transaction as a later statement that USES the new value in this
migration (Postgres restriction on enum additions before 12); this
migration only ADDS values, never uses them within its own `upgrade()`,
so this is safe as an ordinary single-transaction Alembic migration.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0040_chat_message_reports"
down_revision: str | None = "0039_moderation_claims"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_NEW_MODERATION_ACTIONS = [
    "chat_report_dismiss",
    "chat_message_delete",
    "chat_report_escalate_ban",
]
_NEW_TARGET_TYPES = ["chat_message_report"]


def upgrade() -> None:
    for value in _NEW_MODERATION_ACTIONS:
        op.execute(f"ALTER TYPE moderation_action ADD VALUE IF NOT EXISTS '{value}'")
    for value in _NEW_TARGET_TYPES:
        op.execute(f"ALTER TYPE moderation_target_type ADD VALUE IF NOT EXISTS '{value}'")

    chat_report_reason = postgresql.ENUM(
        "spam", "harassment", "inappropriate_content", "other",
        name="chat_report_reason",
    )
    chat_report_reason.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "chat_message_reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
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
    op.create_index("ix_chat_message_reports_message_id", "chat_message_reports", ["message_id"])
    op.create_index(
        "ix_chat_message_reports_status_created_at", "chat_message_reports", ["status", "created_at"]
    )
    op.create_index("ix_chat_message_reports_status", "chat_message_reports", ["status"])

    op.create_table(
        "chat_message_report_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "report_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_message_reports.id", ondelete="CASCADE"),
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
        sa.UniqueConstraint("report_id", "reporter_id", name="uq_chat_report_entry_report_reporter"),
    )
    op.create_index(
        "ix_chat_message_report_entries_report_id", "chat_message_report_entries", ["report_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_chat_message_report_entries_report_id", table_name="chat_message_report_entries")
    op.drop_table("chat_message_report_entries")

    op.drop_index("ix_chat_message_reports_status", table_name="chat_message_reports")
    op.drop_index("ix_chat_message_reports_status_created_at", table_name="chat_message_reports")
    op.drop_index("ix_chat_message_reports_message_id", table_name="chat_message_reports")
    op.drop_table("chat_message_reports")

    postgresql.ENUM(name="chat_report_reason").drop(op.get_bind(), checkfirst=True)

    # DISCLOSED: new `ModerationAction`/`ModerationTargetType` enum VALUES
    # added in `upgrade()` are NOT removed here -- Postgres has no
    # `ALTER TYPE ... DROP VALUE` at all (the only way to truly remove an
    # enum value is to rebuild the type from scratch and rewrite every
    # dependent column), and any `moderation_log_entries` row already
    # written with one of these new action/target values would make that
    # rebuild fail outright. Same posture this codebase already accepts
    # for every other native-enum column addition; downgrading past this
    # migration leaves those two enum types with additional (unused, if
    # no chat-report action was ever logged) valid values -- harmless.
