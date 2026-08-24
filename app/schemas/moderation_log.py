"""Automatic Moderation Activity Log step -- schemas for the two new
Owner-only read endpoints (`GET /admin/activity-log/{user_id}`,
`GET /admin/activity-log`).
"""

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.models.enums import AdminRole, ModerationAction, ModerationTargetType


class ModerationLogEntryOut(BaseModel):
    """One real `ModerationLogEntry` row. `actor_id` is `uuid.UUID | None`
    (not just `uuid.UUID`) -- matches the model's own nullable column
    (`ondelete="SET NULL"`): a log entry whose actor's account was since
    deleted still has a real `actor_role`/`action`/`target`/`outcome` to
    show, just no live actor id. `actor_email`/`actor_full_name` are
    resolved separately (a join, not stored on the log row itself -- see
    `AdminOverrideResponse`'s own precedent for keeping denormalized
    display fields OUT of a model that doesn't own them) and are
    likewise `None` for a deleted actor.

    `actor_role: AdminRole | None` (Repeat-Offender Detection addition,
    migration 0041) -- mirrors the model column's own widening (see
    `app/models/moderation_log.py`'s `actor_role` docstring): `None` now
    means "no human actor," a real, distinct case from "actor account
    was deleted" (`actor_id is None` while `actor_role` still holds the
    real historical role that human once had). The new system-initiated
    `ModerationAction.CHAT_AUTO_BAN` entries are the only rows where
    both are `None`.
    """

    id: uuid.UUID
    actor_id: uuid.UUID | None
    actor_email: str | None
    actor_full_name: str | None
    actor_role: AdminRole | None
    action: ModerationAction
    target_type: ModerationTargetType
    target_id: uuid.UUID
    outcome: dict
    created_at: datetime


class ModerationLogListResponse(BaseModel):
    """Shared response shape for both real read endpoints -- same
    `items`/`total`/`limit`/`offset` field names as
    `PaginatedResponse`/`TeamListResponse` elsewhere in this codebase,
    kept as its OWN model (not a reuse of the generic `PaginatedResponse[T]`)
    only because `ModerationLogEntryOut` needs the two denormalized
    `actor_*` fields resolved outside the ORM row itself -- everything
    else about the shape is identical.
    """

    items: list[ModerationLogEntryOut]
    total: int
    limit: int
    offset: int
