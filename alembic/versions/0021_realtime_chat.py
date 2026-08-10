"""English-Only Gamified Real-time Chat.

Revision ID: 0021_realtime_chat
Revises: 0020_opportunity_aggregation
Create Date: 2026-07-18

Three new tables, fully specified by `app/models/chat.py` -- see that
module's own docstring for the disclosed reasoning behind each one's
exact column set (composite PK on `chat_participants`, no `updated_at`
anywhere in this migration).

Hand-written and reviewed via `alembic upgrade head --sql` /
`alembic downgrade -1 --sql`, matching this project's established
migration-authoring posture (no live Postgres available in this sandbox).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021_realtime_chat"
down_revision: str | None = "0020_opportunity_aggregation"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_rooms",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("is_group", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )

    op.create_table(
        "chat_participants",
        sa.Column(
            "room_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_rooms.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "joined_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    # `user_id` is part of the composite PK above, which already gives it
    # a PK-backed index for lookups scoped to (room_id, user_id) -- this
    # SECOND, single-column index supports the other direction ("every
    # room a given user belongs to"), the query
    # `chat_service.list_my_rooms` actually runs, which the composite PK
    # alone (leftmost-column-first: room_id) cannot serve efficiently.
    op.create_index(
        "ix_chat_participants_user_id", "chat_participants", ["user_id"], unique=False
    )

    op.create_table(
        "chat_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "room_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_rooms.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "sender_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_chat_messages_room_id", "chat_messages", ["room_id"], unique=False)
    op.create_index("ix_chat_messages_sender_id", "chat_messages", ["sender_id"], unique=False)
    # Supports `chat_service.get_message_history`'s "most recent N
    # messages for this room" query -- an index on `room_id` alone would
    # still need a separate sort step over every matching row;
    # `(room_id, created_at)` lets Postgres satisfy "WHERE room_id = ...
    # ORDER BY created_at DESC LIMIT N" directly off the index.
    op.create_index(
        "ix_chat_messages_room_id_created_at", "chat_messages", ["room_id", "created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_chat_messages_room_id_created_at", table_name="chat_messages")
    op.drop_index("ix_chat_messages_sender_id", table_name="chat_messages")
    op.drop_index("ix_chat_messages_room_id", table_name="chat_messages")
    op.drop_table("chat_messages")

    op.drop_index("ix_chat_participants_user_id", table_name="chat_participants")
    op.drop_table("chat_participants")

    op.drop_table("chat_rooms")
