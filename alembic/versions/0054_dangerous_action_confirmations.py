"""Two-Person Confirmation for Dangerous Actions -- `dangerous_action_confirmations`
table, plus new `DANGEROUS_ACTION_REQUESTED`/`DANGEROUS_ACTION_APPROVED`/
`DANGEROUS_ACTION_OVERRIDDEN` `ModerationAction` enum members.

Revision ID: 0054_dangerous_action_confirmations
Revises: 0053_case_notes
Create Date: 2026-09-03

See `app/models/dangerous_action_confirmation.py`'s own module docstring
for the full Step 1/2/3 investigation and design reasoning: STEP 1
confirmed the complete real list of irreversible/destructive actions is
exactly four (VIDEO_DELETE, VIDEO_REJECT_AND_PURGE, CHAT_MESSAGE_DELETE,
GUILD_CONTENT_DELETE); this migration adds the real pending-confirmation
table those four actions now route through, plus the three new,
DISTINCT `ModerationAction` members the request/approve/override
lifecycle itself writes (never reusing the four gated actions' own
members for the lifecycle events, and never reusing `APPROVED` for an
override -- the override path is a separate, always-visible member by
design).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0054_dangerous_action_confirmations"
down_revision: str | None = "0053_case_notes"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_NEW_MODERATION_ACTIONS = [
    "dangerous_action_requested",
    "dangerous_action_approved",
    "dangerous_action_overridden",
]


def upgrade() -> None:
    for value in _NEW_MODERATION_ACTIONS:
        op.execute(f"ALTER TYPE moderation_action ADD VALUE IF NOT EXISTS '{value}'")

    op.create_table(
        "dangerous_action_confirmations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "action",
            postgresql.ENUM(name="moderation_action", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "item_type",
            postgresql.ENUM(name="moderation_target_type", create_type=False),
            nullable=False,
        ),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("is_override", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "requested_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "executed_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_dangerous_action_confirmations_item",
        "dangerous_action_confirmations",
        ["item_type", "item_id", "action", "status"],
    )
    op.create_index(
        "ix_dangerous_action_confirmations_status",
        "dangerous_action_confirmations",
        ["status", "requested_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_dangerous_action_confirmations_status", table_name="dangerous_action_confirmations")
    op.drop_index("ix_dangerous_action_confirmations_item", table_name="dangerous_action_confirmations")
    op.drop_table("dangerous_action_confirmations")
    # No `ALTER TYPE ... DROP VALUE` -- same accepted, disclosed
    # limitation as every other enum-value-adding migration here.
