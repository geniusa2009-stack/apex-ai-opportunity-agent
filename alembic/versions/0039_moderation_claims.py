"""Claim System (Prevent Duplicate Work on the Same Case) --
`claimed_by`/`claimed_at` on `student_videos`.

Revision ID: 0039_moderation_claims
Revises: 0038_moderation_log
Create Date: 2026-08-24

Builds on the confirmed-real tiered role system (0037) and activity log
(0038). See `StudentVideo`'s own column comments
(app/models/student_videos.py) for the full "what is a case" reasoning:
the review queue's only real item kind today is a `StudentVideo` row, so
a claim is two columns added directly to it, not a new polymorphic
table.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0039_moderation_claims"
down_revision: str | None = "0038_moderation_log"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "student_videos",
        sa.Column(
            "claimed_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "student_videos",
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("student_videos", "claimed_at")
    op.drop_column("student_videos", "claimed_by")
