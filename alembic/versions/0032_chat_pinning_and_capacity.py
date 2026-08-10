"""Auto-Assigned English Practice Groups & Chat Room Pinning.

Revision ID: 0032_chat_pinning_and_capacity
Revises: 0031_user_role
Create Date: 2026-07-26

Adds the four columns behind two new chat features, disclosed and
reasoned on the ORM model itself (see `app/models/chat.py`):

- `chat_rooms.is_auto_assigned` (bool, NOT NULL, default false) and
  `chat_rooms.max_members` (nullable int) -- distinguish system-managed
  English Practice groups (capacity-checked, inactivity-swept) from
  ordinary user-created groups/DMs (uncapped, never swept).
- `chat_participants.is_pinned` (bool, NOT NULL, default false) -- a
  per-user, per-room "starred" flag.
- `chat_participants.last_active_at` (timestamptz, NOT NULL, defaults to
  now()) -- drives `chat_group_service.sweep_inactive_participants`'s
  48-hour inactivity window; backfilled to `joined_at`'s own value for
  every pre-existing row so no participant is spuriously swept the
  moment this migration runs.

Hand-written and reviewed via `alembic upgrade head --sql` /
`alembic downgrade -1 --sql`, matching this project's established
migration-authoring posture (no live Postgres available in this
sandbox).
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0032_chat_pinning_and_capacity"
down_revision: str | None = "0031_user_role"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chat_rooms",
        sa.Column("is_auto_assigned", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("chat_rooms", sa.Column("max_members", sa.Integer(), nullable=True))

    op.add_column(
        "chat_participants",
        sa.Column("is_pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "chat_participants",
        sa.Column(
            "last_active_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # Backfill existing rows so `last_active_at` starts equal to
    # `joined_at` rather than the migration's own run time, which would
    # otherwise make every pre-existing participant look freshly active
    # regardless of how long ago they actually joined.
    op.execute("UPDATE chat_participants SET last_active_at = joined_at")


def downgrade() -> None:
    op.drop_column("chat_participants", "last_active_at")
    op.drop_column("chat_participants", "is_pinned")
    op.drop_column("chat_rooms", "max_members")
    op.drop_column("chat_rooms", "is_auto_assigned")
