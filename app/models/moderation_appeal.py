"""Transparency: Notify Users of Moderation Actions + Real Appeal Path.

STEP 1/2 INVESTIGATION, DISCLOSED HERE: confirmed via a full read of
`app/services/notification_service.py` and every real moderation-action
call site that (a) a generic, reusable `create_notification(db, user_id,
type, title, message, payload=None)` already exists and needed no
changes, and (b) exactly ONE of the eight real moderation actions
(`override_video_decision`'s APPROVE branch) notified the affected user
at all -- every negative outcome (reject, permanent delete, reject-and-
purge, chat message delete, chat ban -- both moderator-escalated and the
automatic Arabic-filter one) notified nobody before this task. See
`app/services/moderation_appeal_service.py`'s own module docstring for
the full Step 2/3 design this model backs.

STEP 1, "WHAT IS A CASE" for appeals (same question Phase 4/6 already
answered for videos/chat reports): here it's simpler than the chat-report
case -- there is exactly ONE real submitter per appeal (the actual
affected student, not a crowd of independent reporters), so there is no
separate per-submission entry table the way `ChatMessageReportEntry`
exists; `ModerationAppeal` itself is both the case and the (single) real
submission.
"""

import datetime as dt
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Closed set of case lifecycle states -- plain `String`, same posture as
# `ChatMessageReport`'s own status constants (a status column whose
# value-set could grow, unlike the smaller/stabler enums that get native
# Postgres types).
APPEAL_STATUS_OPEN = "open"
APPEAL_STATUS_UPHELD = "upheld"
APPEAL_STATUS_DENIED = "denied"


class ModerationAppeal(Base):
    """One real appeal against one real `ModerationLogEntry` row.

    `log_entry_id` -- a REAL foreign key (unlike `ModerationLogEntry.
    target_id`'s own deliberately-polymorphic, non-FK column -- see that
    column's own docstring for why THAT one can't be a real FK). This is
    different: a `ModerationLogEntry` row is never deleted (it's an
    append-only audit log -- confirmed by grepping this whole codebase
    for any `DELETE`/`db.delete` against that table; there is none), and
    an appeal is always about exactly one specific, already-written log
    row, not a polymorphic "one of several possible tables" reference --
    a genuine FK is both possible and the more honest, integrity-checked
    choice here. No `ondelete` override (defaults to `RESTRICT`/`NO
    ACTION`) -- since the referenced row is never deleted in practice,
    this is a non-issue in normal operation, and leaving it un-cascading
    is the safer default for a real audit-trail reference.

    `student_id` -- the real affected user submitting the appeal. Not
    necessarily derivable by re-reading the log entry alone at query time
    (see `moderation_appeal_service.resolve_offender_for_entry`'s own
    per-action-type resolution logic, shared with Repeat-Offender
    Detection) -- stored directly here so a resolved appeal keeps a
    stable record of who it was for even if the underlying resolution
    logic's assumptions ever changed. `ondelete="CASCADE"` -- this is the
    ordinary student-owned-workflow-item side (same posture as
    `ChatMessageReportEntry.reporter_id`), not the admin/audit side that
    needs `SET NULL` to survive account deletion.

    `message` -- the student's own real appeal text, required (an appeal
    with no stated reason isn't a real appeal). `additional_context` --
    optional free-text for evidence/context the student wants to add
    (e.g. "I was quoting someone else's message, not writing it myself")
    -- genuinely optional since not every appeal has more to add beyond
    the main message, matching `ChatMessageReportEntry.details`'s own
    "optional supplementary context" precedent. No file/screenshot
    upload field -- this app has no real evidence-upload pipeline for
    ANYTHING today (not videos, not chat reports), so a text-only appeal
    is the realistic scope, not a speculative attachment feature.

    `status` -- open/upheld/denied, same three-state shape as
    `ChatMessageReport.status`'s own real resolution outcomes (just a
    binary decision here instead of three, since an appeal has no
    "message already deleted, nothing left to argue" middle state the
    way a chat report's OWN resolution actions do).

    `claimed_by`/`claimed_at` -- SAME claim-system reuse posture as
    `ChatMessageReport`'s own columns (verbatim copy, same types/FK),
    per this task's own Step 3.2 "should appeals be claimable" question:
    YES -- an appeal is exactly the kind of case two Senior Moderators
    could otherwise duplicate work on (both start drafting a response to
    the same appeal independently), the same real problem the claim
    system was built to solve, and `ModerationAppeal` satisfies
    `moderation_claim_service._Claimable`'s structural Protocol with zero
    new claim code, same as `ChatMessageReport` did.

    `resolved_by`/`resolved_at`/`resolution_notes` -- same denormalized-
    convenience-plus-real-audit-trail split as `ChatMessageReport`'s own
    resolution columns; the REAL permanent record is the
    `ModerationLogEntry` row `moderation_appeals.py`'s resolve endpoint
    writes at the same time (`ModerationAction.APPEAL_UPHELD`/
    `APPEAL_DENIED`).
    """

    __tablename__ = "moderation_appeals"
    __table_args__ = (
        Index("ix_moderation_appeals_log_entry_id", "log_entry_id"),
        Index("ix_moderation_appeals_status_created_at", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    log_entry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("moderation_log_entries.id"), nullable=False
    )

    student_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    message: Mapped[str] = mapped_column(Text, nullable=False)
    additional_context: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default=APPEAL_STATUS_OPEN, index=True)

    claimed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    claimed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Step 3.3's own "be honest about what a reversal can actually
    # restore" requirement, made a real, queryable column rather than
    # something only recoverable by re-deriving it from `action` at read
    # time: `True` only for actions where upholding the appeal can
    # perform a REAL reversal (`VIDEO_OVERRIDE`-into-rejected -> flip back
    # to approved; `CHAT_REPORT_ESCALATE_BAN`/`CHAT_AUTO_BAN` -> real
    # unban). `False` for actions where the underlying content is
    # genuinely gone (`VIDEO_DELETE`, `VIDEO_REJECT_AND_PURGE` -- media
    # purged, `CHAT_MESSAGE_DELETE` -- row hard-deleted) -- upholding
    # those can only acknowledge the mistake, never restore the content.
    # Computed once at appeal-CREATION time (from the real log entry's
    # own `action`) and stored, so the student-facing submission form and
    # the Owner-side queue both read the SAME honest fact without
    # re-deriving it independently.
    can_be_restored: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<ModerationAppeal id={self.id} log_entry_id={self.log_entry_id} status={self.status!r}>"
