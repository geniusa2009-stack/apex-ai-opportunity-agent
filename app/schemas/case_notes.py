"""Internal Notes on Cases -- schemas for
`POST/GET /admin/moderation/{item_type}/{item_id}/notes` and
`DELETE /admin/moderation/{item_type}/{item_id}/notes/{note_id}`."""

import datetime as dt
import uuid

from pydantic import BaseModel, Field

from app.models.case_note import CASE_NOTE_BODY_MAX_LENGTH
from app.models.enums import ModerationTargetType


class CaseNoteCreate(BaseModel):
    body: str = Field(min_length=1, max_length=CASE_NOTE_BODY_MAX_LENGTH)


class CaseNoteOut(BaseModel):
    id: uuid.UUID
    item_type: ModerationTargetType
    item_id: uuid.UUID
    body: str
    author_id: uuid.UUID | None
    author_name: str | None
    created_at: dt.datetime


class CaseNoteListOut(BaseModel):
    items: list[CaseNoteOut]
    total: int
