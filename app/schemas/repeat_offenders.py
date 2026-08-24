"""Repeat-Offender Detection -- schemas for the Owner-only "flagged
users" list (`GET /admin/flagged-users`). See
`app/services/repeat_offender_service.py`'s own module docstring for the
full design reasoning this schema reflects.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel


class FlaggedUserOut(BaseModel):
    """One real user at or above `REPEAT_OFFENDER_THRESHOLD` punitive
    moderation actions within `REPEAT_OFFENDER_LOOKBACK_DAYS`.
    `full_name`/`email` are resolved via a join at query time (not stored
    on this row), same denormalized-display-field posture as
    `ModerationLogEntryOut.actor_email`/`actor_full_name`.
    """

    user_id: uuid.UUID
    full_name: str | None
    email: str | None
    recent_action_count: int


class FlaggedUsersResponse(BaseModel):
    """`GET /admin/flagged-users` response -- a real, pollable list (no
    pagination needed in practice: this is bounded by real repeat
    offenders, not the whole user base), plus the real window/threshold
    it was computed with so the Owner's own client can label it honestly
    (e.g. "3+ actions in the last 30 days") without hardcoding those
    numbers a second time on the frontend.
    """

    items: list[FlaggedUserOut]
    window_days: int
    threshold: int
