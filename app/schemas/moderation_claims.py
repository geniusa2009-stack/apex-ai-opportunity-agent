"""Claim System (Prevent Duplicate Work on the Same Case) step --
schemas for `POST`/`DELETE /admin/moderation/{item_type}/{item_id}/claim`.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.models.enums import ModerationTargetType


class ModerationClaimOut(BaseModel):
    """`POST .../claim` response -- the real, effective claim state
    after the call (always the CALLER as `claimed_by` on success, since
    a conflict raises a 409 instead of returning this). Also reused,
    unmodified, as the shape claim state takes when embedded in
    `AdminVideoReviewOut` (app/schemas/student_videos.py) -- same real
    fields either way."""

    item_type: ModerationTargetType
    item_id: uuid.UUID
    claimed_by: uuid.UUID
    claimed_by_name: str | None
    claimed_at: datetime
