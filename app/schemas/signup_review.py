"""Review of Suspicious New Signups -- schemas for the Owner-only
`GET /admin/signups/review` endpoint. See
`app/services/signup_review_service.py`'s own module docstring for the
full Step 1 investigation (what real signals exist vs. what was
deliberately NOT built) and why each field here is a real, traceable
fact rather than a composite score.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel


class RecentSignupOut(BaseModel):
    """One recently-created account. `reasons` is a real, plain-English
    list built directly from `cohort_size`/`blacklist_trigger_count`/
    `moderation_action_count` -- empty when none of the three real
    signals fired, since a recently-joined account with nothing flagged
    against it is not itself suspicious; it's just recent."""

    user_id: uuid.UUID
    full_name: str | None
    email: str | None
    created_at: datetime
    cohort_size: int
    blacklist_trigger_count: int
    moderation_action_count: int
    reasons: list[str]


class RecentSignupListResponse(BaseModel):
    """`GET /admin/signups/review`'s full response. `total` is the real
    count of accounts within `window_days`, independent of pagination
    (`limit`/`offset`) -- matches this project's own established list-
    response shape (`BlacklistTriggerListResponse`)."""

    items: list[RecentSignupOut]
    total: int
    limit: int
    offset: int
    window_days: int
    velocity_window_minutes: int
    generated_at: datetime
