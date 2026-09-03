"""Escalation Path Between Volunteers -- `moderation_escalations` table,
plus new `ESCALATE`/`ESCALATION_RESOLVE` `ModerationAction` enum
members.

Revision ID: 0051_moderation_escalations
Revises: 0050_opportunity_manual_create_action
Create Date: 2026-09-03

See `app/models/moderation_escalation.py`'s own module docstring for the
full Step 1/2 investigation and design reasoning: a new, small,
genuinely polymorphic table (mirroring `moderation_log_entries`'
target_type/target_id shape, reusing the existing `moderation_target_type`
enum unchanged), NOT five separate migrations bolting escalated_by/
escalated_at columns onto each existing claimable model.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0051_moderation_escalations"
down_revision: str | None = "0050_opportunity_manual_create_action"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_NEW_MODERATION_ACTIONS = ["escalate", "escalation_resolve"]


def upgrade() -> None:
    for value in _NEW_MODERATION_ACTIONS:
        op.execute(f"ALTER TYPE moderation_action ADD VALUE IF NOT EXISTS '{value}'")

    op.create_table(
        "moderation_escalations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "item_type",
            postgresql.ENUM(name="moderation_target_type", create_type=False),
            nullable=False,
        ),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column(
            "escalated_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "escalated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "resolved_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_moderation_escalations_item",
        "moderation_escalations",
        ["item_type", "item_id", "resolved_at"],
    )
    op.create_index(
        "ix_moderation_escalations_resolved_at",
        "moderation_escalations",
        ["resolved_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_moderation_escalations_resolved_at", table_name="moderation_escalations")
    op.drop_index("ix_moderation_escalations_item", table_name="moderation_escalations")
    op.drop_table("moderation_escalations")
    # No `ALTER TYPE ... DROP VALUE` -- same accepted, disclosed
    # limitation as every other enum-value-adding migration here.
