"""Phase 5 (Contribution Stats + The 3 Views) -- schemas for the two new
Owner-only aggregation endpoints (`GET /admin/team/summary`,
`GET /admin/today`). See `app/services/moderation_stats_service.py`'s own
module docstring for the full, disclosed response-time definition this
step's own Step 1 required choosing -- summarized here only as it affects
each field's real meaning.
"""

import datetime as dt
import uuid

from pydantic import BaseModel

from app.models.enums import ModerationAction


class VolunteerPeriodSummary(BaseModel):
    """One row of `GET /admin/team/summary`'s response -- one volunteer's
    real activity over the requested `[start, end)` window.

    `action_counts` -- real per-`ModerationAction` counts (string enum
    value -> count), from a real `GROUP BY actor_id, action` query, NOT
    fetched-all-then-tallied in Python (see the service's own docstring
    for why the data volume doesn't yet justify skipping the DB-side
    aggregation). Only actions this volunteer actually took in the
    window appear as keys -- an action type they never took in this
    window is simply absent, not present with a `0`.

    `avg_response_time_seconds`/`response_time_sample_count` -- the real
    "queue-entry-to-action" gap (`ModerationLogEntry.created_at -
    StudentVideo.created_at`), averaged in SQL, over only the subset of
    this volunteer's actions in the window where that gap is actually
    computable (see the service's own docstring for the disclosed,
    real reasons a `VIDEO_DELETE` action or a `CHAT_UNBAN` action can't
    contribute a sample). `avg_response_time_seconds` is `None` (not
    `0`) when `response_time_sample_count == 0` -- "no real data," not
    "instant response."
    """

    actor_id: uuid.UUID | None
    actor_email: str | None
    actor_full_name: str | None
    action_counts: dict[str, int]
    total_actions: int
    avg_response_time_seconds: float | None
    response_time_sample_count: int


class TeamSummaryResponse(BaseModel):
    """`GET /admin/team/summary` response. `period`/`start`/`end` echo
    back the real, server-computed window this response was aggregated
    over -- see the service's own docstring for the real "week"/"month"
    boundary definitions chosen (and why they're computed server-side
    here, unlike `GET /admin/activity-log`'s deliberately client-computed
    `start`/`end`)."""

    period: str
    start: dt.datetime
    end: dt.datetime
    items: list[VolunteerPeriodSummary]


class TodayDigestResponse(BaseModel):
    """`GET /admin/today` response -- Step 3's single real daily-digest
    payload. See the service's own docstring for the real UTC-calendar-
    day window this uses, and for why `longest_unclaimed_wait_seconds`
    is a LIVE snapshot metric (computed against the review queue's
    CURRENT state at request time), not a historical aggregate the way
    `TeamSummaryResponse`'s fields are.

    `longest_unclaimed_wait_seconds`/`longest_unclaimed_item_id` are
    both `None` together when the review queue currently has no
    unclaimed (or expired-claim) item at all -- an honest "nothing is
    waiting" state, not a `0`-second wait.
    """

    date: dt.date
    window_start: dt.datetime
    window_end: dt.datetime
    total_actions: int
    active_volunteer_count: int
    longest_unclaimed_wait_seconds: float | None
    longest_unclaimed_item_id: uuid.UUID | None


# Re-exported purely so a caller of this schema module doesn't also need
# a separate `app.models.enums` import just to know the real closed set
# of keys `VolunteerPeriodSummary.action_counts` can ever contain.
__all__ = [
    "ModerationAction",
    "TeamSummaryResponse",
    "TodayDigestResponse",
    "VolunteerPeriodSummary",
]
