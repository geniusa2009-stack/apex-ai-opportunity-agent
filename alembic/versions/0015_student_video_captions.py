"""Automated WebVTT Captions Integration.

Revision ID: 0015_student_video_captions
Revises: 0014_student_videos
Create Date: 2026-07-17

Adds the three columns `app/models/student_videos.py`'s module docstring
disclosed at length: `duration_seconds`, `transcript_text`, and
`transcript_segments` (JSONB) on `student_videos`. None of these existed
before this step -- the original Student Video pipeline transcribed
audio and evaluated it with Gemini, but never persisted the transcript
itself or the video's duration anywhere, which made real, time-synced
captions impossible to build. Hand-written and reviewed via
`alembic upgrade head --sql`, matching this project's established
migration-authoring posture (no live Postgres available in this sandbox).

All three columns are nullable, added without a `server_default`,
consistent with every other post-pipeline `student_videos` column added
in `0014` -- existing rows (and any row whose pipeline run predates this
migration) simply have `NULL` here, which the captions endpoint treats
as "no captions available" rather than an error.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_student_video_captions"
down_revision: str | None = "0014_student_videos"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("student_videos", sa.Column("duration_seconds", sa.Integer(), nullable=True))
    op.add_column("student_videos", sa.Column("transcript_text", sa.Text(), nullable=True))
    op.add_column("student_videos", sa.Column("transcript_segments", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("student_videos", "transcript_segments")
    op.drop_column("student_videos", "transcript_text")
    op.drop_column("student_videos", "duration_seconds")
