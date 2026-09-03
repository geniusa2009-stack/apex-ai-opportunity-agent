"""Canned Responses / Templates -- schemas for the Owner-only CRUD
endpoints and the Moderator+-visible list/use endpoints."""

import datetime as dt
import uuid

from pydantic import BaseModel, Field

from app.models.canned_response import CANNED_RESPONSE_BODY_MAX_LENGTH, CANNED_RESPONSE_TITLE_MAX_LENGTH
from app.models.enums import ModerationTargetType


class CannedResponseCreate(BaseModel):
    title: str = Field(min_length=1, max_length=CANNED_RESPONSE_TITLE_MAX_LENGTH)
    body: str = Field(min_length=1, max_length=CANNED_RESPONSE_BODY_MAX_LENGTH)
    applies_to: ModerationTargetType | None = None


class CannedResponseUpdate(BaseModel):
    """All fields optional -- only what's sent is changed, same
    established "partial update" convention `ShiftAssignmentUpdate`/
    `AdminOpportunityCreate`'s sibling schemas already use."""

    title: str | None = Field(default=None, min_length=1, max_length=CANNED_RESPONSE_TITLE_MAX_LENGTH)
    body: str | None = Field(default=None, min_length=1, max_length=CANNED_RESPONSE_BODY_MAX_LENGTH)
    applies_to: ModerationTargetType | None = None
    clear_applies_to: bool = Field(
        default=False,
        description="Set true to clear applies_to back to general-purpose (null); "
        "omitting applies_to alone leaves the existing value untouched.",
    )


class CannedResponseOut(BaseModel):
    id: uuid.UUID
    title: str
    body: str
    applies_to: ModerationTargetType | None
    created_by: uuid.UUID | None
    times_used: int
    created_at: dt.datetime
    updated_at: dt.datetime

    model_config = {"from_attributes": True}


class CannedResponseUseOut(BaseModel):
    """`POST .../use` response -- the real, current template state
    (including the now-incremented `times_used`) plus nothing else: the
    caller already has `body` from this same response to copy into
    whichever action's own `notes` field they're filling (see
    `app/models/canned_response.py`'s own Step 3.3 integration
    reasoning)."""

    template: CannedResponseOut
