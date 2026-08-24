"""Extend Moderation to Chat Messages (Reporting + Second Real Queue).

Builds on the confirmed-real admin panel: tiered roles (39d1d0f), activity
log (5d2934e), advisory claims (47906f0), stats + 3 views backend
(5881cfc), wired UI (004fc92). This is the "second queue" extension point
`app/api/v1/endpoints/admin_claims.py`'s own module docstring explicitly
left open ("a genuinely new claimable queue in the future is an entry in
`_CLAIMABLE_MODELS`, not a new pair of endpoints").

STEP 1/2 INVESTIGATION, DISCLOSED HERE (see this task's own real findings
before any of this was written): neither a user-facing "report this
message" mechanism NOR a reviewable queue populated from one existed
anywhere in this codebase. `app/services/chat_security.py`'s Arabic-
detection filter does not create anything reviewable either -- a message
that trips it is auto-banned and NEVER reaches `chat_messages` at all (see
`app/api/v1/endpoints/chat.py`'s WebSocket handler, step 4: the ban+close
happens BEFORE `chat_service.save_message` is ever called). "Reviewing
reported messages" is therefore genuinely new end to end: a new report
endpoint, a new queue, and new resolution actions -- not a UI layer over
something that already existed.

STEP 1, "WHAT IS A CASE" (this task's own required question, same one
Phase 4's `admin_claims.py` had to answer for videos) -- here the answer
is NOT "one row per report submission." A single message can legitimately
receive reports from several different users, and a moderator opening the
review queue needs to see and act on ONE case per reported message, not a
duplicate queue row per reporter. So the shape is two real tables:

- `ChatMessageReport` -- the CASE. One row per (message, currently-open
  reporting episode). This is the thing a moderator "opens," claims (via
  the EXACT SAME `claimed_by`/`claimed_at` columns/pattern
  `StudentVideo` already established -- see `ModerationTargetType.
  CHAT_MESSAGE_REPORT`'s own docstring), and resolves. Exactly one OPEN
  case may exist per message at a time (enforced in
  `app/services/chat_report_service.py`, not a DB partial-unique index --
  SQLite, this codebase's own real test/seed backend per the disclosed
  pip/import gap, has no partial-unique-index support, and the real
  invariant only needs to hold at write time under this codebase's
  existing "service enforces, model stores" split, same posture as
  `moderation_claim_service.py`'s own expiry logic).
- `ChatMessageReportEntry` -- one row per INDIVIDUAL user's report
  submission against a case. `UNIQUE(report_id, reporter_id)` is the
  real, enforced anti-mass-report rule -- see
  `app/services/chat_report_service.py`'s own module docstring for the
  full threshold/rate-limiting reasoning.

Claim reuse, literally: `ChatMessageReport.claimed_by`/`.claimed_at` are
the SAME two columns, same types, same `ondelete="SET NULL"` FK, same
"lazy read-time expiry via `moderation_claim_service.get_effective_claim`"
semantics as `StudentVideo`'s own claim columns -- this class only needs
to satisfy `moderation_claim_service._Claimable`'s structural Protocol
(`claimed_by`/`claimed_at` attributes) to be claimable through the
EXISTING `POST`/`DELETE /admin/moderation/{item_type}/{item_id}/claim`
endpoints with zero new claim code. See `app/api/v1/endpoints/
admin_claims.py`'s own `_CLAIMABLE_MODELS` dict for the one-line
registration this enables.
"""

import datetime as dt
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import ChatReportReason

# Closed set of case lifecycle states -- plain `String`, not a native
# Postgres enum, matching `StudentVideoStatus`'s own established
# precedent (a status column whose value-set is expected to keep growing
# as new resolution actions are added, unlike the smaller/stabler
# `ChatReportReason`/`AdminRole` which DO get native enums). See
# `StudentVideoStatus`'s own docstring (this module's sibling) for the
# fuller "why plain String, not native ENUM" reasoning this reuses
# verbatim.
REPORT_STATUS_OPEN = "open"
REPORT_STATUS_DISMISSED = "dismissed"
REPORT_STATUS_MESSAGE_DELETED = "message_deleted"
REPORT_STATUS_ESCALATED_BAN = "escalated_ban"


class ChatMessageReport(Base):
    """The reportable CASE -- see this module's own docstring for why
    this, not `ChatMessageReportEntry`, is the row the claim system and
    the admin review-queue/resolution actions all operate on.

    `message_id` has NO `unique=True` at the column level -- a message
    CAN accumulate more than one `ChatMessageReport` row over its
    lifetime (e.g. dismissed once, later re-reported by someone else and
    opened again) -- only "no two SIMULTANEOUSLY OPEN cases for the same
    message" is a real invariant, and that's a service-layer check
    (`chat_report_service.create_report`), not something a single-column
    DB constraint can express without a partial index.
    """

    __tablename__ = "chat_message_reports"
    __table_args__ = (
        Index("ix_chat_message_reports_message_id", "message_id"),
        Index("ix_chat_message_reports_status_created_at", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_messages.id", ondelete="CASCADE"), nullable=False
    )

    # Plain String -- see module-level status constants above.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=REPORT_STATUS_OPEN, index=True)

    # --- Claim System reuse -- SAME columns/types/FK posture as
    # `StudentVideo.claimed_by`/`.claimed_at` (app/models/student_videos.py),
    # copied deliberately verbatim rather than introducing a differently-
    # shaped claim concept for this second queue. See this module's own
    # docstring.
    claimed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    claimed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Resolution bookkeeping -- who resolved this case (dismiss / delete
    # message / escalate to ban) and when/how. `ondelete="SET NULL"`,
    # same "audit trail must survive the resolver's account being
    # deleted" reasoning as `ModerationLogEntry.actor_id`'s own docstring
    # -- this column is a convenience denormalization for the queue
    # listing (avoids a join against `ModerationLogEntry` just to show
    # "resolved by X" inline); the REAL, permanent record of the action
    # is still the `ModerationLogEntry` row `admin_chat_reports.py`
    # writes at the same time, same "not best-effort, same transaction"
    # posture as every other logged action in this codebase.
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<ChatMessageReport id={self.id} message_id={self.message_id} status={self.status!r}>"


class ChatMessageReportEntry(Base):
    """One INDIVIDUAL user's report submission against a
    `ChatMessageReport` case. `UNIQUE(report_id, reporter_id)` is the
    real anti-mass-report enforcement -- see
    `app/services/chat_report_service.py`'s own module docstring for the
    full reasoning on why this (not a report-count threshold before the
    case surfaces in the queue) is the real defense this task's own ask
    asks for.

    `reporter_id` uses `ondelete="CASCADE"` (not `SET NULL`) -- unlike
    `ModerationLogEntry.actor_id`/`ChatMessageReport.resolved_by` (which
    are the ADMIN side of an audit trail that must survive account
    deletion), this is the ordinary-user side: same posture as
    `UserBookmark.user_id`/every other plain user-owned-content FK in
    this codebase -- if a reporting user's account is deleted, there is
    no real reason for their individual report row to survive as a
    dangling reference (the CASE itself, and any moderator action taken
    on it, is unaffected either way -- only this one entry row would be
    removed, and the case's other reporters' own entries are untouched).
    """

    __tablename__ = "chat_message_report_entries"
    __table_args__ = (
        UniqueConstraint("report_id", "reporter_id", name="uq_chat_report_entry_report_reporter"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    report_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chat_message_reports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reporter_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    reason: Mapped[ChatReportReason] = mapped_column(
        SAEnum(
            ChatReportReason,
            name="chat_report_reason",
            native_enum=True,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
    )
    # Free-text context, e.g. required-in-spirit (not DB-enforced) when
    # `reason == ChatReportReason.OTHER` -- see that enum member's own
    # docstring. Optional for the other three reasons, which are
    # self-explanatory categories a reporter may not have anything to add
    # to.
    details: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<ChatMessageReportEntry id={self.id} report_id={self.report_id} reporter_id={self.reporter_id}>"
