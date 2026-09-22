"""Temporal Opportunity State Machine + Change History -- adds
`opportunities.lifecycle_state`/`lifecycle_state_reason`/
`lifecycle_state_evaluated_at` and the new, append-only
`opportunity_change_history` table.

Revision ID: 0061_temporal_state_and_change_history
Revises: 0060_opportunity_type_expansion
Create Date: 2026-09-22

See `app/models/opportunity.py`'s own updated docstring (the
`lifecycle_state` column block) and `app/models/opportunity_change_
history.py`'s own module docstring for the full Part 0/B/C design
reasoning. `lifecycle_state` is a plain `String(30)` column (NOT a
native Postgres enum), same established convention this repo already
uses for every other lifecycle/status column
(`opportunities.review_status`, `chat_message_report_entries.status`-
adjacent columns, etc.) -- no `ALTER TYPE` needed for this migration.

Purely additive: three new nullable-or-defaulted columns on
`opportunities` (every existing row gets `lifecycle_state='discovered'`
via `server_default`, never silently reinterpreted as already
OPEN/CLOSED) and one new table. No existing column is dropped, renamed,
or has its type changed. No existing `opportunities` row's `title`/
`type`/`deadline`/`country`/etc. is rewritten by this migration.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0061_temporal_state_and_change_history"
down_revision: str | None = "0060_opportunity_type_expansion"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "opportunities",
        sa.Column("lifecycle_state", sa.String(length=30), nullable=False, server_default="discovered"),
    )
    op.add_column("opportunities", sa.Column("lifecycle_state_reason", sa.Text(), nullable=True))
    op.add_column(
        "opportunities", sa.Column("lifecycle_state_evaluated_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index("ix_opportunities_lifecycle_state", "opportunities", ["lifecycle_state"])

    op.create_table(
        "opportunity_change_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "opportunity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("opportunities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("field_name", sa.String(length=50), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sources.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "evidence_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("evidence.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("change_reason", sa.Text(), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_opportunity_change_history_opportunity_id", "opportunity_change_history", ["opportunity_id"]
    )
    op.create_index("ix_opportunity_change_history_changed_at", "opportunity_change_history", ["changed_at"])
    op.create_index("ix_opportunity_change_history_source_id", "opportunity_change_history", ["source_id"])


def downgrade() -> None:
    op.drop_index("ix_opportunity_change_history_source_id", table_name="opportunity_change_history")
    op.drop_index("ix_opportunity_change_history_changed_at", table_name="opportunity_change_history")
    op.drop_index("ix_opportunity_change_history_opportunity_id", table_name="opportunity_change_history")
    op.drop_table("opportunity_change_history")

    op.drop_index("ix_opportunities_lifecycle_state", table_name="opportunities")
    op.drop_column("opportunities", "lifecycle_state_evaluated_at")
    op.drop_column("opportunities", "lifecycle_state_reason")
    op.drop_column("opportunities", "lifecycle_state")
