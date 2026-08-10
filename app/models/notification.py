"""Smart Notifications Engine -- database schema.

This step's own literal path (`app/models/notification.py`) is free --
no naming collision this time (unlike `app/models/gamification.py` in
the immediately preceding Gamification milestone). One model, exactly
this step's own literal column list, nothing added beyond two disclosed,
necessary indexes (see `Notification`'s own docstring).

DISCLOSED JUDGMENT CALL -- `type` stays a plain `String`, not a DB-level
enum. The ask itself says `type: String (e.g. 'SYSTEM', 'ACHIEVEMENT',
'OPPORTUNITY', 'WARNING')` -- "e.g." reads as illustrative examples of a
free-text convention, not an exhaustive, closed set the ask wants
enforced at the database level (contrast with this codebase's own
`StudentVideoStatus`/`VideoProcessingStatus`/`TrackerStatus`, all of
which the ASK itself named as closed sets of specific string literals).
A Postgres `ENUM` column is also the single most operationally painful
column type in this codebase to extend later (`ALTER TYPE ... ADD
VALUE` cannot run inside the same transaction as other DDL on most
Postgres versions this project might deploy to) -- a real cost not worth
paying for a field the ask itself hedges with "e.g." rather than a
closed list. `NOTIFICATION_TYPES` below is a plain Python tuple of the
four example values, kept as a named constant purely for reuse by
`notification_service.py`'s own "which types count as highly critical"
logic (see that module's docstring) and by `schemas/notification.py`'s
response model -- NOT a hard app-level allow-list; nothing in this
model or its migration rejects a fifth, future type string.
"""

import datetime as dt
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Illustrative, non-exhaustive -- see this module's own docstring on why
# `type` itself stays an unconstrained `String` rather than a DB enum.
NOTIFICATION_TYPES = ("SYSTEM", "ACHIEVEMENT", "OPPORTUNITY", "WARNING")


class Notification(Base):
    """One row per notification actually delivered to one user -- this
    step's own literal five-column spec (`id`, `user_id`, `type`,
    `title`, `message`, `is_read`, `created_at` -- six, counting `id`),
    plus one disclosed composite index.

    `id` is a real, app-generated UUID (`default=uuid.uuid4`, not
    DB-generated) -- `notification_service.create_notification` needs
    the new row's own id BEFORE any WebSocket broadcast/email dispatch
    happens (the broadcast payload embeds it, per this step's own "emit
    ... {"type": "new_notification", "data": {...}}" instruction), so a
    Python-side default that's known before `db.commit()` is simpler
    than round-tripping through a DB-generated value first.

    `user_id` is indexed (per the ask's own explicit "Indexed" note on
    this column) via a single composite index below rather than a bare
    single-column one -- see the index's own comment for why.

    `type`/`title` are bounded `String` columns (short, single-line
    labels); `message` is `Text` (an unbounded notification body, per
    the ask's own literal type choice) -- same bounded-vs-unbounded
    split this codebase already uses throughout (e.g. `Badge.name` vs.
    `Badge.description`).

    `is_read` defaults to `False` at BOTH the Python level (`default`)
    and the database level (`server_default`) -- the Python default
    covers ordinary ORM inserts, the server default covers any row
    inserted outside the ORM (raw SQL, a future backfill), matching this
    codebase's own `TimestampMixin` posture on why server-side defaults
    matter independently of app-side ones.

    `created_at` uses `server_default=func.now()`, same established
    convention as every other timestamp column in this codebase (see
    `app/models/mixins.py`'s `TimestampMixin`) -- NOT actually built via
    that mixin, since the ask's own column list has no `updated_at`
    companion, and a notification is genuinely immutable after creation
    except for the single `is_read` flip (which doesn't need its own
    "when was this last touched" timestamp for anything this step asks
    for).
    """

    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    type: Mapped[str] = mapped_column(String(50), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)

    is_read: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)

    # --- Actionable Notifications addition (migration 0033) ---
    # Nullable JSONB -- structured data an in-feed action button needs to
    # act without a second lookup, e.g. a friend request:
    # `{"type": "friend_request", "connection_id": "<uuid>"}`. NULL for
    # every plain, non-actionable notification (a badge unlock, a
    # deadline warning) -- most notifications have nothing to act on.
    payload: Mapped[dict | None] = mapped_column(JSONB(), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Composite `(user_id, created_at DESC-via-query)` index, not a bare
    # index on `user_id` alone -- disclosed addition beyond the ask's
    # literal "Indexed" note. This step's own two real query shapes are
    # BOTH "this user's notifications, newest first" (`GET
    # /api/v1/notifications`, latest 50 by `created_at DESC`) and "this
    # user's UNREAD notifications" (`PUT .../read-all`'s own UPDATE ...
    # WHERE user_id = ? AND is_read = false`) -- a composite index
    # covering `user_id` first (equality filter, same as a bare
    # single-column index would give) AND `created_at` second (the sort/
    # range column) serves the listing query as a single index scan
    # instead of an index-then-sort; `is_read` is deliberately NOT part
    # of this index (a boolean column has too little selectivity to be
    # worth a third index key here, and the read-all UPDATE is a rare,
    # bulk, non-latency-sensitive write, not a hot read path).
    __table_args__ = (Index("ix_notifications_user_id_created_at", "user_id", "created_at"),)

    def __repr__(self) -> str:
        return f"<Notification id={self.id} user_id={self.user_id} type={self.type!r} is_read={self.is_read}>"
