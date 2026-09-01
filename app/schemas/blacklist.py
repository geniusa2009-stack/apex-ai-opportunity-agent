"""General Content Blacklist / Pattern Auto-Trigger -- schemas for the
Owner-only pattern-CRUD endpoints and the Moderator+-visible trigger-
event view."""

import datetime as dt
import uuid

from pydantic import BaseModel, Field

from app.models.blacklist import PATTERN_MAX_LENGTH


class BlacklistedPatternCreate(BaseModel):
    pattern: str = Field(min_length=1, max_length=PATTERN_MAX_LENGTH)
    is_regex: bool = False
    description: str | None = Field(default=None, max_length=500)


class BlacklistedPatternOut(BaseModel):
    id: uuid.UUID
    pattern: str
    is_regex: bool
    description: str | None
    created_by: uuid.UUID | None
    created_at: dt.datetime

    model_config = {"from_attributes": True}


class BlacklistTriggerOut(BaseModel):
    """One real trigger event -- Moderator+ visible (see
    `app/api/v1/endpoints/admin_blacklist.py`'s own module docstring for
    the Owner-edits/Moderator-views tier split this reflects). Reuses
    `ModerationLogEntry`'s own real fields; the matched pattern's TEXT is
    included here deliberately (this is an internal, trusted-staff view,
    not the end-user-facing rejection message -- see that endpoint
    module's own docstring for why those two audiences see different
    amounts of detail)."""

    id: uuid.UUID
    target_user_id: uuid.UUID
    content_type: str
    matched_pattern: str
    content_snippet: str
    created_at: dt.datetime


class BlacklistTriggerListResponse(BaseModel):
    items: list[BlacklistTriggerOut]
    total: int
    limit: int
    offset: int
