"""Escalation Path Between Volunteers -- schemas for
`POST/DELETE /admin/moderation/{item_type}/{item_id}/escalate` and
`GET /admin/escalations`.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import ModerationTargetType


class EscalationRaiseRequest(BaseModel):
    """`POST .../escalate` body -- the one real piece of information a
    Moderator raising an escalation must supply (see
    `app/models/moderation_escalation.py`'s own `note` column
    docstring for why this is required, not optional)."""

    note: str = Field(min_length=1, max_length=2000)


class EscalationOut(BaseModel):
    """Real, current state of one escalation row -- returned by
    `POST`/`DELETE .../escalate`, and as each item of
    `GET /admin/escalations`'s own listing."""

    id: uuid.UUID
    item_type: ModerationTargetType
    item_id: uuid.UUID
    note: str
    escalated_by: uuid.UUID | None
    escalated_by_name: str | None
    escalated_at: datetime
    resolved_by: uuid.UUID | None
    resolved_by_name: str | None
    resolved_at: datetime | None


class EscalationListOut(BaseModel):
    items: list[EscalationOut]
    total: int
