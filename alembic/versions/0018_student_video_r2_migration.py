"""Student Video Pinata -> Cloudflare R2 Migration.

Revision ID: 0018_student_video_r2_migration
Revises: 0017_admin_moderation
Create Date: 2026-07-17

Renames `student_videos.hls_master_cid` -> `hls_master_url` and
`student_videos.thumbnail_cid` -> `thumbnail_url`, and widens both from
`VARCHAR(255)` to `VARCHAR(500)` -- see `app/models/student_videos.py`'s
own module docstring and each column's comment for the full disclosed
reasoning (both used to hold a real IPFS CID; both now hold a full,
directly-playable Cloudflare R2 public URL, which has more length
headroom than a bare CID string).

`op.alter_column` (a real, in-place rename via Postgres's own `ALTER
TABLE ... RENAME COLUMN`) is used here rather than `drop_column` +
`add_column` -- this PRESERVES every existing row's already-resolved
CID value in place under its new column name, rather than silently
discarding it. Disclosed, deliberate: an existing row's OLD Pinata CID
is not itself a valid R2 URL, so it will not actually be PLAYABLE via
this renamed column post-migration (nothing in this migration
re-uploads existing pinned content to R2 -- that's a real, separate
backfill concern, not something a schema migration alone can or should
do) -- but preserving the raw value is still strictly better than
irrecoverably deleting it: it documents that this row WAS pinned to IPFS
under that CID (still resolvable directly against any IPFS gateway by
hand, e.g. `https://gateway.pinata.cloud/ipfs/{old_value}/master.m3u8`,
even though this codebase's own frontend no longer knows to construct
that URL for a Student Video row after this migration). See the
README's disclosed "Not included" note on this exact gap.

Hand-written and reviewed via `alembic upgrade head --sql` /
`alembic downgrade -1 --sql`, matching this project's established
migration-authoring posture (no live Postgres available in this
sandbox).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_student_video_r2_migration"
down_revision: str | None = "0017_admin_moderation"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "student_videos",
        "hls_master_cid",
        new_column_name="hls_master_url",
        existing_type=sa.String(length=255),
        type_=sa.String(length=500),
        existing_nullable=True,
    )
    op.alter_column(
        "student_videos",
        "thumbnail_cid",
        new_column_name="thumbnail_url",
        existing_type=sa.String(length=255),
        type_=sa.String(length=500),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "student_videos",
        "thumbnail_url",
        new_column_name="thumbnail_cid",
        existing_type=sa.String(length=500),
        type_=sa.String(length=255),
        existing_nullable=True,
    )
    op.alter_column(
        "student_videos",
        "hls_master_url",
        new_column_name="hls_master_cid",
        existing_type=sa.String(length=500),
        type_=sa.String(length=255),
        existing_nullable=True,
    )
