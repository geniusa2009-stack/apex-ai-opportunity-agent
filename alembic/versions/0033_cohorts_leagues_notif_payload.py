"""Course Cohorts, Weekly Leagues, and Actionable Notifications.

Revision ID: 0033_cohorts_leagues_notif
Revises: 0032_chat_pinning_and_capacity
Create Date: 2026-07-26

Disclosed fix (found during this rebuild's Phase 0 boot check): the
original revision id here, `0033_cohorts_leagues_notif_payload`, is 34
characters -- one past the 32-character width of Alembic's default
`alembic_version.version_num` column, which made `alembic upgrade head`
fail for real with `StringDataRightTruncation` on a real Postgres, not
just in a sandbox stand-in. Shortened to fit. Safe to rename outright
(rather than leave it and widen the column) because nothing had this
migration applied anywhere yet -- confirmed no other revision file
references the old id as its `down_revision` before renaming.

Three independent, unrelated column additions, bundled in one migration
since none needs its own data backfill beyond a server_default:

- `chat_rooms.course_id` (nullable FK -> courses.id) -- links an
  auto-created "Course Cohort" room to its course. Nullable: English
  Practice groups and user-created groups/DMs have no course.
- `chat_participants.is_muted` (bool, default false) -- per-member mute
  flag, independent of `is_pinned`.
- `chat_messages.is_bot` (bool, default false) -- marks a message as
  posted by the AI Bot (distinct message-bubble styling), not a real
  student.
- `gamification_profiles.weekly_xp` (int, default 0) and `.league`
  (string, default 'bronze') -- the Weekly League ladder, independent of
  the existing all-time `total_xp`.
- `notifications.payload` (nullable JSONB) -- structured data for
  in-feed actionable notifications (e.g. a friend-request's
  `connection_id`), so the frontend can act without a second lookup.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0033_cohorts_leagues_notif"
down_revision: str | None = "0032_chat_pinning_and_capacity"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chat_rooms", sa.Column("course_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_chat_rooms_course_id", "chat_rooms", "courses", ["course_id"], ["id"], ondelete="CASCADE"
    )
    op.create_index("ix_chat_rooms_course_id", "chat_rooms", ["course_id"])

    op.add_column(
        "chat_participants",
        sa.Column("is_muted", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "chat_messages",
        sa.Column("is_bot", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    op.add_column(
        "gamification_profiles",
        sa.Column("weekly_xp", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "gamification_profiles",
        sa.Column("league", sa.String(length=20), nullable=False, server_default="bronze"),
    )

    op.add_column("notifications", sa.Column("payload", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("notifications", "payload")
    op.drop_column("gamification_profiles", "league")
    op.drop_column("gamification_profiles", "weekly_xp")
    op.drop_column("chat_messages", "is_bot")
    op.drop_column("chat_participants", "is_muted")
    op.drop_index("ix_chat_rooms_course_id", table_name="chat_rooms")
    op.drop_constraint("fk_chat_rooms_course_id", "chat_rooms", type_="foreignkey")
    op.drop_column("chat_rooms", "course_id")
