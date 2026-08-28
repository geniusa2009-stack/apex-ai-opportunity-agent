"""High-Risk-Content View -- schemas for the Owner-only
`GET /admin/high-risk` aggregation endpoint. See
`app/services/high_risk_service.py`'s own module docstring for the full
Step 1 investigation (what already existed vs. what was genuinely new)
and the explainability design this schema reflects: every item below
carries its own plain, real `reason` string derived directly from the
underlying fact, never an opaque composite score.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel


class HighRiskFlaggedUserOut(BaseModel):
    """One real repeat offender -- same underlying fact
    `GET /admin/flagged-users` already surfaces (reused, not
    re-derived), reshaped here with an explicit `reason` string so this
    view is self-explanatory without the Owner needing to already know
    what `recent_action_count` means."""

    user_id: uuid.UUID
    full_name: str | None
    email: str | None
    recent_action_count: int
    reason: str


class HighRiskBannedUserOut(BaseModel):
    """One real, currently-active chat ban (live Redis truth via
    `chat_security.list_banned_users` -- the first real caller of that
    already-existing function; see the service module's own docstring).
    """

    user_id: uuid.UUID
    full_name: str | None
    email: str | None
    remaining_seconds: int
    reason: str


class HighRiskQueueBacklogOut(BaseModel):
    """One real queue's current backlog -- either the open chat-report
    queue or the pending-video-review queue. `count`/`oldest_item_at`
    are the exact same underlying facts `GET /admin/chat/reports` and
    `GET /admin/videos/review-queue` already expose per-item; this is
    just the aggregate shape (how many, how long has the oldest one
    waited) rather than the full paginated list."""

    count: int
    oldest_item_at: datetime | None
    reason: str


class HighRiskViewResponse(BaseModel):
    """`GET /admin/high-risk`'s full response -- four real, independently
    explainable signal groups, no unified opaque "risk score." A real,
    pollable view (per this task's own explicit pull-based, no-push-
    notification constraint) -- the Owner opens this deliberately; it is
    never surfaced via a badge, alert, or count that implies something
    is unread.
    """

    flagged_users: list[HighRiskFlaggedUserOut]
    banned_users: list[HighRiskBannedUserOut]
    open_chat_reports: HighRiskQueueBacklogOut
    pending_video_reviews: HighRiskQueueBacklogOut
    window_days: int
    threshold: int
    generated_at: datetime
