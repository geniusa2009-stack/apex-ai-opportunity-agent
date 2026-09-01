"""Review of Guild Content -- report models.

STEP 1 INVESTIGATION, DISCLOSED (this task's own real findings, kept
here as the durable record):

1. `guild_service._moderate_content` (a real Gemini-backed safety/PII
   check) already runs on every `create_post`/`submit_comment_with_rewrite`
   call, BEFORE the row is ever persisted -- confirmed by re-reading
   `guild_service.py` in full. It is purely automatic and block-only: it
   either lets the content through unremarked, or raises
   `ContentRejectedError` (a real 422), and the rejected text is NEVER
   saved anywhere. It produces no reviewable trace at all -- there is
   nothing here for a human moderator to look at, by construction.
2. The General Content Blacklist / Pattern Auto-Trigger phase already
   wires `blacklist_service.check_content`/`log_trigger` into both
   `create_post` and `submit_comment_with_rewrite`, writing a real
   `ModerationLogEntry` (`action=BLACKLIST_TRIGGERED`) for every guild
   blacklist rejection. Confirmed this is ALREADY visible today: `GET
   /admin/blacklist/triggers` is a real, unfiltered-by-content-type query
   over every `BLACKLIST_TRIGGERED` entry, and `admin-moderation.html`'s
   existing Blacklist tab already has real `guild_post`/`guild_comment`/
   `guild_rewrite` labels wired into `BLACKLIST_CONTENT_TYPE_LABELS`. No
   new visibility work is needed for this half of the feature -- it
   shipped, unnoticed as "guild coverage," in the immediately prior
   phase.
3. Genuinely missing, confirmed via a full grep for
   `report`/`Report`/`flag` across `app/models`/`app/services`/
   `app/api`: there is no way today for a user to report a specific,
   already-PUBLISHED guild post or comment -- distinct from the two
   automatic pre-publish checks above, which only ever see content
   before it exists as a row. This is the real, narrow gap this module
   closes, mirroring `app/models/chat_moderation.py`'s own
   `ChatMessageReport`/`ChatMessageReportEntry` shape closely (see that
   module's own docstring for the full "case vs. individual submission"
   reasoning, reused verbatim here rather than re-derived): a single
   post or comment can legitimately receive reports from more than one
   student, and a moderator needs to see and act on ONE case per
   reported item, not a duplicate queue row per reporter.

DESIGN DECISIONS THIS MODULE MAKES, DISCLOSED:

- One polymorphic report table covers BOTH posts and comments
  (`target_type` + `target_id`), rather than two separate tables --
  unlike chat, which only ever reports one kind of thing (a message),
  guild content has two reportable kinds. `target_id` is deliberately
  NOT a foreign key, same "no single real table it always points to"
  reasoning `ModerationLogEntry.target_id`'s own docstring already
  established for this exact situation -- a discriminator column
  (`target_type`, plain string, reusing the same literal
  `"guild_post"`/`"guild_comment"` values the blacklist's own
  `content_type` field already uses, for one consistent vocabulary
  across both features) makes a stored id unambiguous.
- `reason` reuses `ChatReportReason` (spam/harassment/inappropriate_
  content/other) UNCHANGED rather than a near-duplicate `GuildReportReason`
  -- these are genuinely generic content-report categories, not
  chat-specific in wording or meaning, and this codebase's own "don't
  duplicate a working closed set" discipline (see e.g.
  `repeat_offender_service.get_offense_events_since` reusing
  `_resolve_offenders` unchanged) applies here too.
- Claim System reuse, literally: `GuildContentReport.claimed_by`/
  `.claimed_at` are the SAME two columns/types/FK posture as
  `ChatMessageReport`'s own -- this class only needs to satisfy
  `moderation_claim_service._Claimable`'s structural Protocol to become
  claimable through the EXISTING claim endpoints with zero new claim
  code (see `app/api/v1/endpoints/admin_claims.py`'s own
  `_CLAIMABLE_MODELS` dict for the one-line registration this enables).
"""

import datetime as dt
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import ChatReportReason

REPORT_STATUS_OPEN = "open"
REPORT_STATUS_DISMISSED = "dismissed"
REPORT_STATUS_CONTENT_DELETED = "content_deleted"

# Same literal vocabulary `blacklist_service`'s own `content_type` field
# already uses for guild content -- one consistent set of strings across
# both features rather than two independently-named ones for the exact
# same two real target kinds.
TARGET_TYPE_POST = "guild_post"
TARGET_TYPE_COMMENT = "guild_comment"


class GuildContentReport(Base):
    """The reportable CASE -- see this module's own docstring for the
    full "why one polymorphic table, not two" reasoning. Exactly one OPEN
    case may exist per (target_type, target_id) at a time, enforced in
    `app/services/guild_report_service.py` (application-layer check, same
    "service enforces, model stores" posture `ChatMessageReport`'s own
    docstring already established for the identical SQLite-partial-index
    limitation) -- NOT a DB constraint.
    """

    __tablename__ = "guild_content_reports"
    __table_args__ = (
        Index("ix_guild_content_reports_target", "target_type", "target_id"),
        Index("ix_guild_content_reports_status_created_at", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    target_type: Mapped[str] = mapped_column(String(20), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default=REPORT_STATUS_OPEN, index=True)

    # --- Claim System reuse -- SAME columns/types/FK posture as
    # `ChatMessageReport.claimed_by`/`.claimed_at`. See module docstring.
    claimed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    claimed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<GuildContentReport id={self.id} target={self.target_type}:{self.target_id} status={self.status!r}>"


class GuildContentReportEntry(Base):
    """One individual user's report submission against a
    `GuildContentReport` case. `UNIQUE(report_id, reporter_id)` is the
    real anti-mass-report enforcement -- same rule, same reasoning, as
    `ChatMessageReportEntry`'s own `UniqueConstraint`.
    """

    __tablename__ = "guild_content_report_entries"
    __table_args__ = (
        UniqueConstraint("report_id", "reporter_id", name="uq_guild_report_entry_report_reporter"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    report_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("guild_content_reports.id", ondelete="CASCADE"), nullable=False, index=True
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
    details: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<GuildContentReportEntry id={self.id} report_id={self.report_id} reporter_id={self.reporter_id}>"
