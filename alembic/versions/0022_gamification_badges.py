"""Gamification: Daily Streaks, Badges, and Leaderboard.

Revision ID: 0022_gamification_badges
Revises: 0021_realtime_chat
Create Date: 2026-07-18

Three new tables, fully specified by `app/models/badges.py` -- see that
module's own docstring for the disclosed reasoning behind: (a) why these
models live in a NEW file/migration rather than extending the
pre-existing, differently-scoped `app/models/gamification.py` /
`student_accountability` table (Ecosystem Feature 4's Token Economy),
and (b) the composite primary key on `user_badges` (no separate
surrogate `id` column, per this step's own literal 3-column spec).

Hand-written and reviewed via `alembic upgrade head --sql` /
`alembic downgrade -1 --sql`, matching this project's established
migration-authoring posture (no live Postgres available in this sandbox).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022_gamification_badges"
down_revision: str | None = "0021_realtime_chat"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "gamification_profiles",
        # `user_id` is BOTH the primary key and the FK to `users.id` --
        # same genuine-1:1 shape as `student_accountability`, per this
        # step's own exact spec for this model (see app/models/badges.py's
        # docstring).
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("total_xp", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("current_streak", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("highest_streak", sa.Integer(), nullable=False, server_default="0"),
        # Plain DATE (not DateTime/timestamp) -- per this step's own
        # literal column list; see app/models/badges.py's docstring.
        sa.Column("last_activity_date", sa.Date(), nullable=True),
    )

    op.create_table(
        "badges",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("icon_url", sa.String(length=1000), nullable=True),
        sa.Column("required_xp", sa.Integer(), nullable=False),
    )
    # Real, disclosed addition beyond the ask's literal column list --
    # two badges sharing a name is a data-integrity bug, not a
    # legitimate case; see app/models/badges.py's own docstring.
    op.create_unique_constraint("uq_badges_name", "badges", ["name"])
    op.create_index("ix_badges_required_xp", "badges", ["required_xp"])

    op.create_table(
        "user_badges",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "badge_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("badges.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "awarded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )

    # Leaderboard query (`ORDER BY total_xp DESC LIMIT 10`) is the
    # highest-traffic read this step's own ask defines -- a real,
    # disclosed addition beyond the bare column list, same "an ask's
    # own literal spec doesn't preclude an obviously-needed index"
    # posture already established elsewhere in this codebase (e.g.
    # `Badge.required_xp` above).
    op.create_index(
        "ix_gamification_profiles_total_xp", "gamification_profiles", ["total_xp"]
    )


def downgrade() -> None:
    op.drop_index("ix_gamification_profiles_total_xp", table_name="gamification_profiles")
    op.drop_table("user_badges")
    op.drop_index("ix_badges_required_xp", table_name="badges")
    op.drop_constraint("uq_badges_name", "badges", type_="unique")
    op.drop_table("badges")
    op.drop_table("gamification_profiles")
