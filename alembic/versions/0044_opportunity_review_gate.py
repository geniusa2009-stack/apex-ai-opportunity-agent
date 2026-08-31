"""Review of Aggregated Opportunities Before Publish -- real pre-publish
gate on `Opportunity`, plus Claim System reuse.

Revision ID: 0044_opportunity_review_gate
Revises: 0043_manual_timed_bans
Create Date: 2026-08-31

Step 1 investigation confirmed a real gap: no human ever saw a scraped/
aggregated opportunity before it went live -- `is_verified` was flipped
straight to `True` by the AI verification task alone
(app/workers/tasks.py::_verify_new_opportunities) once its confidence
score cleared a threshold. See `OpportunityReviewStatus`'s own docstring
(app/models/enums.py) for the full reasoning on the new `review_status`
column this migration adds, and `Opportunity.review_status`'s own column
docstring (app/models/opportunity.py) for why this migration explicitly
backfills already-`is_verified=True` rows to `PUBLISHED` rather than
letting the new column's own PENDING_REVIEW default silently apply to
them -- an opportunity a student could already see before this migration
must not become invisible the moment it runs.

`claimed_by`/`claimed_at` -- Claim System reuse (Step 3 decision, see
`app/api/v1/endpoints/admin_opportunities.py`'s own module docstring),
identical shape to migration 0039's `student_videos.claimed_by`/
`.claimed_at` and migration 0040's `chat_message_reports` pair.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0044_opportunity_review_gate"
down_revision: str | None = "0043_manual_timed_bans"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "opportunities",
        sa.Column(
            "review_status",
            sa.String(length=20),
            nullable=False,
            server_default="pending_review",
        ),
    )
    op.create_index(
        "ix_opportunities_review_status", "opportunities", ["review_status"]
    )

    # Backfill: an opportunity that was ALREADY verified-and-visible
    # before this migration stays visible -- explicit one-time UPDATE,
    # not the column's own PENDING_REVIEW default (which only governs
    # rows that don't get this UPDATE, i.e. new rows created after this
    # migration runs).
    op.execute(
        "UPDATE opportunities SET review_status = 'published' WHERE is_verified = true"
    )

    op.add_column(
        "opportunities",
        sa.Column(
            "claimed_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "opportunities",
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("opportunities", "claimed_at")
    op.drop_column("opportunities", "claimed_by")
    op.drop_index("ix_opportunities_review_status", table_name="opportunities")
    op.drop_column("opportunities", "review_status")
