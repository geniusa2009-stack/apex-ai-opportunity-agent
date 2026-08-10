"""English-Only Gamified Real-time Chat -- database models.

Three models, fully named by this step's own ask: `ChatRoom`,
`ChatParticipant`, `ChatMessage`. This module's own docstring covers the
disclosed judgment calls behind each one's exact column set; per-model
detail lives on each class below.

NAMING COLLISION, DISCLOSED: this codebase already has an UNRELATED
pre-existing feature also called "chat" -- `app/api/v1/endpoints/chat.py`
(`POST /chat`, `app/services/chatbot.py`) is a stateless AI Q&A assistant
(LangChain + Groq), with no database model of its own at all (its
`ChatMessage`/`ChatRequest` are request/response Pydantic schemas, not
ORM rows). This step's `ChatRoom`/`ChatParticipant`/`ChatMessage` are a
completely different, real, persisted peer-to-peer messaging system.
There is no class-name collision at the Python level (the AI chatbot's
`ChatMessage` lives in `app/api/v1/endpoints/chat.py` as a plain
`pydantic.BaseModel`, never imported into `app.models`; this module's
`ChatMessage` is the new SQLAlchemy model, imported into `app.models`
instead) -- but see `app/api/v1/endpoints/chat.py`'s own updated module
docstring for how the WebSocket/REST ROUTES for this new feature were
placed to avoid colliding with that pre-existing router's `/chat` prefix.

TIMESTAMP-COLUMN DEVIATION FROM THIS CODEBASE'S USUAL CONVENTION,
DISCLOSED: most models in this codebase apply `TimestampMixin`
(`created_at` + `updated_at`) uniformly, even to append-only/log-like
rows (e.g. `AgentAlert`). This step's own ask is unusually literal and
explicit about each model's exact column list (`ChatRoom`: "id, name,
is_group, created_at"; `ChatParticipant`: "room_id, user_id, joined_at";
`ChatMessage`: "id, room_id, sender_id, content, created_at") -- none of
which mentions `updated_at`. Since a chat message is never edited in this
ask (no edit/delete endpoint exists), and a room's/participant's "last
modified" timestamp has no defined meaning here either, silently adding
`TimestampMixin` to every table would introduce three unrequested
`updated_at` columns with no real use. `TimestampMixin` is deliberately
NOT used here; each model hand-writes only the single timestamp column
its own ask literally names.
"""

import datetime as dt
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class ChatRoom(Base):
    """A 1-on-1 conversation or a study group. `is_group` distinguishes
    the two (per this step's own literal field list) rather than a
    separate `room_type` enum -- the ask names exactly one boolean field
    for this, nothing more granular.

    `name` is nullable: a 1-on-1 DM has no user-supplied name in this
    ask's own flow (the frontend's "Create Group" modal is the only
    place a name is ever collected; starting a DM just needs the other
    participant, no name prompt) -- disclosed necessary nullability, not
    a literal ask detail, since forcing every DM to have a name would
    mean inventing one out of nowhere for every 1-on-1 chat.
    """

    __tablename__ = "chat_rooms"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_group: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # --- Auto-Assigned English Practice Groups additions (migration 0032) ---
    # `is_auto_assigned` marks a room as one of the system-managed "English
    # Practice" groups (see `app/services/chat_group_service.py`) rather
    # than a user-created group/DM -- distinct from `is_group` (which only
    # says 1:1 vs. multi-party): a user-created study group also has
    # `is_group=True` but `is_auto_assigned=False`. Only auto-assigned rooms
    # are ever capacity-checked against `max_members` or swept for
    # inactivity; a user-created group has no member cap.
    is_auto_assigned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Nullable -- only meaningful (and only ever set) on an auto-assigned
    # room; a DM or a user-created group has no capacity concept at all,
    # so `NULL` there (not e.g. 0, which would misleadingly read as
    # "full") is the honest "not applicable" value.
    max_members: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- AI-Powered Course Cohorts addition (migration 0033) ---
    # Nullable FK to the course this auto-created cohort room belongs to.
    # NULL for English Practice groups, DMs, and user-created groups --
    # only a course-cohort room (one per enrolled course, auto-created on
    # first enrollment, see `chat_group_service.get_or_create_course_cohort_room`)
    # ever sets this.
    course_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("courses.id", ondelete="CASCADE"), nullable=True, index=True
    )

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    participants: Mapped[list["ChatParticipant"]] = relationship(
        back_populates="room", cascade="all, delete-orphan", lazy="selectin"
    )
    messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="room", cascade="all, delete-orphan", lazy="noload"
    )

    def __repr__(self) -> str:
        return f"<ChatRoom id={self.id} name={self.name!r} is_group={self.is_group}>"


class ChatParticipant(Base):
    """Junction row: one (room, user) membership. Composite primary key
    on `(room_id, user_id)` -- this step's own literal field list for
    this model ("room_id, user_id, joined_at") never mentions a surrogate
    `id` column, unlike every other multi-row-per-user table in this
    codebase (`UserConnection`, `AchievementCongrats`, etc., which DO get
    a disclosed surrogate `id` because the ask's own field list for THOSE
    was silent on a primary key entirely). Here the ask gives exactly two
    columns that already form a natural, non-redundant identity for a
    junction table -- a user can only be a member of a given room once --
    so a composite PK is used instead of inventing an unrequested `id`
    column. This also does double duty as the uniqueness guard preventing
    the same user from being added to the same room twice.
    """

    __tablename__ = "chat_participants"

    room_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_rooms.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )

    joined_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # --- Chat Room Pinning & Inactivity Sweep additions (migration 0032) ---
    # `is_pinned` is a PER-(user, room) flag, not a column on `ChatRoom`
    # itself -- pinning is a personal organizational preference ("I starred
    # this conversation"), not a property of the room shared by every
    # participant, so it belongs on the junction row exactly like
    # `joined_at` already does.
    is_pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # This participant's last activity IN THIS ROOM (touched whenever they
    # send a message here -- see `chat_service.save_message`'s call site in
    # the WebSocket handler). Defaults to `joined_at`'s own value at insert
    # time (a freshly-joined member isn't already "inactive"); only ever
    # read by `chat_group_service.sweep_inactive_participants`, and only
    # for participants of an `is_auto_assigned=True` room -- a manually
    # created group/DM is never swept, so this column is harmlessly unused
    # there.
    last_active_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # --- AI-Powered Course Cohorts addition (migration 0033) ---
    # Per-member mute: a muted room still shows in the sidebar and its
    # messages still save/broadcast normally, but the client suppresses
    # unread badges/push notices for it -- independent of `is_pinned`
    # (a room can be both pinned and muted).
    is_muted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    room: Mapped["ChatRoom"] = relationship(back_populates="participants")

    def __repr__(self) -> str:
        return f"<ChatParticipant room_id={self.room_id} user_id={self.user_id}>"


class ChatMessage(Base):
    """One saved, English-only message. Per this step's own logic flow,
    a row is only ever INSERTed here for text that has ALREADY passed the
    Arabic filter (`app/services/chat_security.py`'s `contains_arabic`) --
    messages that trip the filter never reach the database at all; the
    sender is banned and disconnected instead. See
    `app/api/v1/endpoints/chat.py`'s WebSocket handler for that flow.

    `sender_id` uses `ondelete="CASCADE"` (not `SET NULL`) -- same
    reasoning as every other user-owned-content FK in this codebase
    (`AgentAlert.user_id`, `UserConnection.user_id`): this ask's own
    field list has `sender_id` as a plain, non-nullable column, and a
    `NOT NULL` column can't accept `SET NULL` on delete regardless.
    """

    __tablename__ = "chat_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    room_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_rooms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sender_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    content: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    # --- AI-Powered Course Cohorts addition (migration 0033) ---
    # True only for a message posted by the system "APEX AI Bot" (see
    # `chat_group_service.answer_stale_course_questions`) -- `sender_id`
    # still points at a real system user row (a plain FK NOT NULL column
    # can't be nullable here), but the frontend uses this flag to render
    # a visually distinct bot bubble instead of resolving it from
    # `sender_id` alone.
    is_bot: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    room: Mapped["ChatRoom"] = relationship(back_populates="messages")

    def __repr__(self) -> str:
        return f"<ChatMessage id={self.id} room_id={self.room_id} sender_id={self.sender_id}>"
