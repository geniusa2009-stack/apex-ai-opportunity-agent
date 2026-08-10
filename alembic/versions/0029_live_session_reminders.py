"""Live Session Reminders -- `live_sessions.reminder_sent` column.

Revision ID: 0029_live_session_reminders
Revises: 0028_live_sessions
Create Date: 2026-07-19

Follow-up to `0028_live_sessions`, closing a disclosed gap from that
step's own README entry ("no scheduled-time reminder notification for a
confirmed booking"). One new column, added the exact same way
`0016_video_thumbnails`-style single-column follow-up migrations in this
project already do: `op.add_column`, `server_default` so the column is
non-nullable from day one without requiring a manual backfill step for
whatever rows already exist.

Hand-written and reviewed via `alembic upgrade head --sql` /
`alembic downgrade -1 --sql`, matching this project's established
migration-authoring posture (no live Postgres available in this sandbox).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_live_session_reminders"
down_revision: str | None = "0028_live_sessions"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "live_sessions",
        sa.Column(
            "reminder_sent",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )


def downgrade() -> None:
    op.drop_column("live_sessions", "reminder_sent")
