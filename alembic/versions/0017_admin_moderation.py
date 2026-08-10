"""Admin Override Dashboard & AI Decision Correction System.

Revision ID: 0017_admin_moderation
Revises: 0016_video_thumbnails
Create Date: 2026-07-17

Disclosed numbering note: this step's own ask itself already anticipates
the exact situation that came up in the immediately preceding step
("Provide the Alembic migration script 0016 (or next sequential)") --
`0016` is already claimed by `0016_video_thumbnails.py`, so this
continues the chain as `0017`, chained via `down_revision` below.

Adds the two columns `app/models/student_videos.py`'s own docstring
disclosed at length: `admin_override` (`Boolean NOT NULL DEFAULT false`
-- a real, non-null default is used here, unlike most prior nullable
post-hoc columns in this chain, because this one has an unambiguous
"nothing has happened yet" value that isn't `NULL`: every existing row
genuinely has never been admin-overridden, so `false` is not a guess,
it's a fact) and `admin_notes` (`String`, nullable -- no admin reason
exists for any row until the first override call against it).

Hand-written and reviewed via `alembic upgrade head --sql` /
`alembic downgrade -1 --sql`, matching this project's established
migration-authoring posture (no live Postgres available in this
sandbox).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_admin_moderation"
down_revision: str | None = "0016_video_thumbnails"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "student_videos",
        sa.Column("admin_override", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("student_videos", sa.Column("admin_notes", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("student_videos", "admin_notes")
    op.drop_column("student_videos", "admin_override")
