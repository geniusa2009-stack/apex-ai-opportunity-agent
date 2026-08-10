"""Video Analytics, Watch Time Tracking & Engagement Engine.

Revision ID: 0019_video_analytics
Revises: 0018_student_video_r2_migration
Create Date: 2026-07-17

Creates `video_playback_logs` -- one row per (user, video) pair, tracking
`last_position_seconds`/`cumulative_watch_time_seconds`/`is_completed`
for both the Student UGC Video pipeline and the Course/Lesson pipeline
via a polymorphic `(video_id, video_type)` pair. See
`app/models/analytics.py`'s own module docstring for the full disclosed
reasoning behind every column/constraint choice below -- summarized here:

- `video_id` carries NO foreign key (it points into either
  `student_videos.id` or `lessons.id` depending on the sibling
  `video_type` column -- Postgres has no native "conditional FK"
  mechanism for that).
- `user_id` DOES carry a real `ON DELETE CASCADE` foreign key to
  `users.id`, but stays nullable at the schema level (this milestone's
  own literal ask: "nullable for public unregistered views if needed").
- `uq_video_playback_log_user_video`, a real unique constraint on
  `(user_id, video_id, video_type)`, is what makes the heartbeat
  endpoint's atomic upsert logic actually safe under concurrent
  requests -- without it, two racing heartbeats for the same student/
  video could each insert their own row instead of one being correctly
  recognized as an update.

Hand-written and reviewed via `alembic upgrade head --sql` /
`alembic downgrade -1 --sql`, matching this project's established
migration-authoring posture (no live Postgres available in this sandbox).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0019_video_analytics"
down_revision: str | None = "0018_student_video_r2_migration"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "video_playback_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("video_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("video_type", sa.String(length=20), nullable=False),
        sa.Column("last_position_seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column("cumulative_watch_time_seconds", sa.Float(), nullable=False, server_default="0"),
        sa.Column("is_completed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "user_id", "video_id", "video_type", name="uq_video_playback_log_user_video"
        ),
    )
    op.create_index(
        "ix_video_playback_logs_user_id", "video_playback_logs", ["user_id"], unique=False
    )
    op.create_index(
        "ix_video_playback_logs_video_id", "video_playback_logs", ["video_id"], unique=False
    )
    op.create_index(
        "ix_video_playback_logs_user_video",
        "video_playback_logs",
        ["user_id", "video_id", "video_type"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_video_playback_logs_user_video", table_name="video_playback_logs")
    op.drop_index("ix_video_playback_logs_video_id", table_name="video_playback_logs")
    op.drop_index("ix_video_playback_logs_user_id", table_name="video_playback_logs")
    op.drop_table("video_playback_logs")
