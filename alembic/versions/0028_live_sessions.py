"""Live 1:1 Video Sessions.

Revision ID: 0028_live_sessions
Revises: 0027_hyper_personalized_platform
Create Date: 2026-07-19

One new table, fully specified by `app/models/live_session.py` -- see
that module's own docstring for the disclosed reasoning behind the
column set (single-sided rating, `requested_by`/`requested_with` naming
instead of a fixed mentor/mentee direction).

Hand-written and reviewed via `alembic upgrade head --sql` /
`alembic downgrade -1 --sql`, matching this project's established
migration-authoring posture (no live Postgres available in this sandbox).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0028_live_sessions"
down_revision: str | None = "0027_hyper_personalized_platform"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "live_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "requested_by_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "requested_with_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="requested"),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("topic", sa.String(length=255), nullable=True),
        sa.Column(
            "requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("feedback", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "requested_by_id != requested_with_id", name="ck_live_session_distinct_parties"
        ),
        sa.CheckConstraint(
            "rating IS NULL OR (rating >= 1 AND rating <= 5)", name="ck_live_session_rating_range"
        ),
    )
    op.create_index(
        "ix_live_sessions_requested_by_id", "live_sessions", ["requested_by_id"], unique=False
    )
    op.create_index(
        "ix_live_sessions_requested_with_id", "live_sessions", ["requested_with_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_live_sessions_requested_with_id", table_name="live_sessions")
    op.drop_index("ix_live_sessions_requested_by_id", table_name="live_sessions")
    op.drop_table("live_sessions")
