"""Smart Notifications Engine.

Revision ID: 0023_notifications
Revises: 0022_gamification_badges
Create Date: 2026-07-18

One new table, fully specified by `app/models/notification.py` -- see
that module's own docstring for the disclosed reasoning behind: (a)
`type` staying a plain, unconstrained `String` rather than a DB enum,
and (b) the one composite `(user_id, created_at)` index this migration
adds beyond the ask's own bare column list.

Hand-written and reviewed via `alembic upgrade head --sql` /
`alembic downgrade -1 --sql`, matching this project's established
migration-authoring posture (no live Postgres available in this
sandbox).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023_notifications"
down_revision: str | None = "0022_gamification_badges"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        # App-generated UUID (not DB-generated) -- `notification_service.
        # create_notification` needs the new row's own id before the
        # WebSocket broadcast payload is built; see the model's own
        # docstring.
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.String(length=50), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("is_read", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )

    # Covers BOTH this step's own real query shapes ("this user's
    # notifications, newest first" and "this user's unread notifications")
    # as a single index scan -- see the model's own docstring for why
    # `is_read` is deliberately NOT a third key here.
    op.create_index(
        "ix_notifications_user_id_created_at", "notifications", ["user_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_user_id_created_at", table_name="notifications")
    op.drop_table("notifications")
