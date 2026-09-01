"""Review of Guild Content -- Pydantic schemas.

Covers both the user-facing report submission
(`POST /guilds/posts/{post_id}/report`,
`POST /guilds/posts/{post_id}/comments/{comment_id}/report`) and the
admin-facing report queue/resolution endpoints
(`app/api/v1/endpoints/admin_guild_reports.py`). Mirrors
`app/schemas/chat_moderation.py`'s own conventions closely -- see that
module's docstring.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import ChatReportReason

# ---------------------------------------------------------------------
# User-facing: submitting a report
# ---------------------------------------------------------------------


class GuildContentReportRequest(BaseModel):
    """Backs both report-submission endpoints."""

    reason: ChatReportReason
    details: str | None = Field(default=None, max_length=1000)


class GuildContentReportResponse(BaseModel):
    """Real, honest acknowledgment -- mirrors
    `ChatMessageReportResponse` field-for-field, target_type/target_id
    in place of message_id."""

    report_id: uuid.UUID
    target_type: str
    target_id: uuid.UUID
    reason: ChatReportReason
    created_at: datetime
    total_reports: int = Field(description="Distinct reporters on this content's currently open case, including this one.")


# ---------------------------------------------------------------------
# Admin-facing: the report queue + resolution actions
# ---------------------------------------------------------------------


class AdminGuildReportEntryOut(BaseModel):
    reporter_id: uuid.UUID
    reporter_name: str | None
    reason: ChatReportReason
    details: str | None
    created_at: datetime


class AdminGuildContentOut(BaseModel):
    """The reported post/comment's own real content, embedded in each
    queue item -- mirrors `AdminChatMessageOut`. `null` when the target
    row no longer exists (a real, disclosed edge case -- see
    `admin_guild_reports.py`'s own module docstring)."""

    id: uuid.UUID
    author_id: uuid.UUID
    author_name: str | None
    content: str
    created_at: datetime


class AdminGuildReportOut(BaseModel):
    """One row of `GET /admin/guild/reports`."""

    id: uuid.UUID
    target_type: str
    target_id: uuid.UUID
    content: AdminGuildContentOut | None
    status: str
    reports: list[AdminGuildReportEntryOut]
    report_count: int
    first_reported_at: datetime

    claimed_by: uuid.UUID | None
    claimed_by_name: str | None
    claimed_at: datetime | None

    resolved_by_name: str | None = None
    resolved_at: datetime | None = None
    resolution_notes: str | None = None

    # Repeat-Offender Detection reuse -- same real, inline "how many
    # times has this content's AUTHOR been moderated recently" context
    # as `AdminChatReportOut.offender_recent_action_count`.
    offender_recent_action_count: int = 0


class AdminGuildReportDismissRequest(BaseModel):
    notes: str | None = Field(default=None, max_length=2000)


class AdminGuildReportDeleteContentRequest(BaseModel):
    notes: str | None = Field(default=None, max_length=2000)


class AdminGuildReportActionResponse(BaseModel):
    success: bool
    report_id: uuid.UUID
    status: str
