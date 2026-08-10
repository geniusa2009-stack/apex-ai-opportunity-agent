"""Hybrid Video Thumbnail System (Custom User Upload + Automatic FFmpeg
Fallback Extraction).

Revision ID: 0016_video_thumbnails
Revises: 0015_student_video_captions
Create Date: 2026-07-17

Disclosed numbering deviation: this step's own ask literally labels this
migration "0015" -- but `0015_student_video_captions.py` (the immediately
preceding step, Automated WebVTT Captions Integration) already claimed
that number in this same migration chain. Continuing the established
sequential-numbering convention (every prior step's migration follows the
one before it, never reusing a number) rather than creating a duplicate
"0015", this is "0016", chained onto "0015_student_video_captions" via
`down_revision` below.

Adds one nullable `thumbnail_cid: String(255)` column to THREE tables --
`courses`, `lessons`, `student_videos` -- per this step's own explicit
"Ensure Course, Lesson, and StudentVideo tables have a nullable
thumbnail_cid field" instruction. See each model's own docstring
(app/models/courses.py point 5, app/models/student_videos.py) for the
per-table disclosed reasoning: only `student_videos.thumbnail_cid` is
actually populated by a real pipeline in this pass (the router + Celery
task's custom-upload-or-FFmpeg-fallback logic); `courses.thumbnail_cid`
and `lessons.thumbnail_cid` exist to satisfy this step's literal schema
requirement, with no admin write path wired up to populate them yet.

Hand-written and reviewed via `alembic upgrade head --sql` /
`alembic downgrade -1 --sql`, matching this project's established
migration-authoring posture (no live Postgres available in this sandbox).
No `server_default` on any of the three -- consistent with every other
nullable post-hoc column added in this codebase (e.g. 0015's own three
columns): existing rows simply read `NULL` for a column that didn't exist
until this migration ran.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_video_thumbnails"
down_revision: str | None = "0015_student_video_captions"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("courses", sa.Column("thumbnail_cid", sa.String(length=255), nullable=True))
    op.add_column("lessons", sa.Column("thumbnail_cid", sa.String(length=255), nullable=True))
    op.add_column("student_videos", sa.Column("thumbnail_cid", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("student_videos", "thumbnail_cid")
    op.drop_column("lessons", "thumbnail_cid")
    op.drop_column("courses", "thumbnail_cid")
