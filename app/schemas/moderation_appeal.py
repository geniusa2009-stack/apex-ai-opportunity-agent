"""Transparency: Notify Users of Moderation Actions + Real Appeal Path --
Pydantic schemas. Mirrors `app/schemas/chat_moderation.py`'s own
conventions closely, per this codebase's established "new queue/case
schemas mirror the most recent precedent" posture.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models.enums import ModerationAction, ModerationTargetType

# ---------------------------------------------------------------------
# Student-facing: submitting an appeal
# ---------------------------------------------------------------------


class AppealSubmitRequest(BaseModel):
    """Backs `POST /moderation/appeals`."""

    log_entry_id: uuid.UUID
    message: str = Field(min_length=1, max_length=2000)
    additional_context: str | None = Field(
        default=None,
        max_length=2000,
        description="Optional supplementary evidence/context -- e.g. \"I was quoting someone "
        "else's message, not writing it myself.\" Genuinely optional, same posture as "
        "ChatMessageReportEntry.details.",
    )


class AppealOut(BaseModel):
    """One real appeal, as seen by BOTH the submitting student (`GET
    /moderation/appeals/mine`) and a reviewing Senior Moderator+ (`GET
    /admin/appeals`) -- one shared shape, no separate student/admin
    response models, since nothing here is sensitive to hide from the
    student who submitted it (unlike `AdminChatReportEntryOut`, which
    hides OTHER reporters' identities from the general public but not
    from a moderator)."""

    id: uuid.UUID
    log_entry_id: uuid.UUID
    action: ModerationAction
    target_type: ModerationTargetType
    student_id: uuid.UUID
    student_name: str | None
    message: str
    additional_context: str | None
    status: str
    can_be_restored: bool

    claimed_by: uuid.UUID | None
    claimed_by_name: str | None
    claimed_at: datetime | None

    resolved_by_name: str | None = None
    resolved_at: datetime | None = None
    resolution_notes: str | None = None

    created_at: datetime


class AppealResolveRequest(BaseModel):
    """`POST /admin/appeals/{appeal_id}/resolve` body."""

    decision: Literal["uphold", "deny"]
    notes: str | None = Field(default=None, max_length=2000)


class AppealResolveResponse(BaseModel):
    """Real, honest resolution outcome -- `reversal_applied` is `False`
    for an upheld appeal against a genuinely-unrecoverable action
    (content already deleted/purged), NOT just for a denied one, so a
    caller can't mistake "we agreed you were right" for "and we restored
    it" when those are two different real facts (see
    `app/services/moderation_appeal_service.py`'s own module docstring).
    """

    success: bool
    appeal_id: uuid.UUID
    status: str
    reversal_applied: bool
    reversal_detail: str | None = None
