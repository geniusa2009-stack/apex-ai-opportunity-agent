"""Extend Moderation to Chat Messages -- Pydantic schemas.

Covers both the user-facing report submission
(`POST /chat/rooms/{room_id}/messages/{message_id}/report`) and the
admin-facing reported-message queue/resolution endpoints
(`app/api/v1/endpoints/admin_chat_reports.py`). Mirrors
`app/schemas/student_videos.py`'s own `AdminVideoReviewOut`/
`AdminOverrideRequest`/`AdminOverrideResponse` conventions closely, per
this task's own "mirroring the video review-queue's shape/conventions
closely" instruction.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import ChatReportReason


# ---------------------------------------------------------------------
# User-facing: submitting a report
# ---------------------------------------------------------------------


class ChatMessageReportRequest(BaseModel):
    """Backs `POST /chat/rooms/{room_id}/messages/{message_id}/report`."""

    reason: ChatReportReason
    details: str | None = Field(
        default=None,
        max_length=1000,
        description="Optional free-text context. Effectively required when reason=OTHER, "
        "since that category has no self-explanatory meaning on its own -- not enforced at "
        "the schema layer (a reporter typing nothing under real distress shouldn't be blocked "
        "from filing the report at all), but surfaced as-is to the moderator either way.",
    )


class ChatMessageReportResponse(BaseModel):
    """`POST .../report`'s response -- real, honest acknowledgment, not
    just a 204. `total_reports` lets the reporting user's own client (if
    it chooses to show it) reflect that others have also flagged the same
    message, without exposing WHO."""

    report_id: uuid.UUID
    message_id: uuid.UUID
    reason: ChatReportReason
    created_at: datetime
    total_reports: int = Field(description="Distinct reporters on this message's currently open case, including this one.")


# ---------------------------------------------------------------------
# Admin-facing: the reported-message queue + resolution actions
# ---------------------------------------------------------------------


class AdminChatReportEntryOut(BaseModel):
    """One individual report submission, as shown to a moderator inside
    a queue item -- moderators see who reported and why (this is an
    internal team tool, not a public-facing report), unlike the
    reporting user's own response above, which never exposes other
    reporters."""

    reporter_id: uuid.UUID
    reporter_name: str | None
    reason: ChatReportReason
    details: str | None
    created_at: datetime


class AdminChatMessageOut(BaseModel):
    """The reported message's own real content/context, embedded in each
    queue item -- mirrors `AdminVideoReviewOut` embedding the video's own
    real fields directly rather than making the moderator open a second
    endpoint to see what's actually being reviewed."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    room_id: uuid.UUID
    sender_id: uuid.UUID
    sender_name: str | None
    content: str
    created_at: datetime
    is_bot: bool


class AdminChatReportOut(BaseModel):
    """One row of `GET /admin/chat/reports` -- the reported-message
    queue's real item shape, mirroring `AdminVideoReviewOut`'s own
    conventions: real embedded content, real claim state (SAME shape as
    `ModerationClaimOut`'s own fields, reused field-for-field rather than
    renamed), real resolution state once resolved.
    """

    id: uuid.UUID
    message: AdminChatMessageOut
    status: str
    reports: list[AdminChatReportEntryOut]
    report_count: int
    first_reported_at: datetime

    claimed_by: uuid.UUID | None
    claimed_by_name: str | None
    claimed_at: datetime | None

    resolved_by_name: str | None = None
    resolved_at: datetime | None = None
    resolution_notes: str | None = None

    # --- Repeat-Offender Detection addition (migration 0041) ---
    # Same real, inline "how many times has THIS message's sender been
    # moderated recently" context as `AdminVideoReviewOut.
    # offender_recent_action_count` -- see that field's own docstring and
    # `app/services/repeat_offender_service.py`'s module docstring for the
    # shared reasoning. Keyed off `message.sender_id`, not the reporter.
    offender_recent_action_count: int = 0


class AdminChatReportDismissRequest(BaseModel):
    """`POST /admin/chat/reports/{report_id}/dismiss` body."""

    notes: str | None = Field(default=None, max_length=2000)


class AdminChatReportDeleteMessageRequest(BaseModel):
    """`DELETE /admin/chat/reports/{report_id}/message` request body --
    Transparency: Notify Users of Moderation Actions addition. DISCLOSED,
    REAL GAP this closes: this endpoint previously took NO body at all.
    Same optional-reason shape/reasoning as `AdminChatReportDismissRequest.
    notes` -- added so the sender's own notification can carry a real,
    specific reason beyond the report's own category, not to gate the
    action on it.
    """

    notes: str | None = Field(default=None, max_length=2000)


class AdminChatReportEscalateBanRequest(BaseModel):
    """`POST /admin/chat/reports/{report_id}/escalate-ban` body."""

    notes: str | None = Field(default=None, max_length=2000)
    duration_seconds: int = Field(
        default=1800,
        ge=60,
        le=86400,
        description="Real Redis TTL passed straight to chat_security.ban_user -- defaults to "
        "the same 1800s (30 min) this codebase already uses for the automatic Arabic-filter ban, "
        "not a separate hardcoded duration a moderator has no way to reason about.",
    )


class AdminChatReportActionResponse(BaseModel):
    """Shared response shape for all three resolution actions -- dismiss,
    delete-message, escalate-ban. `banned_until_seconds` is only ever
    populated by the escalate-ban action (`None` otherwise, per this
    schema's own honest-nullability posture, same as `AdminVideoReviewOut.
    ai_score` etc.)."""

    success: bool
    report_id: uuid.UUID
    status: str
    banned_until_seconds: int | None = None
