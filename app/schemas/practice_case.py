"""Training / Practice Mode for New Volunteers -- Pydantic schemas.

Backs `app/api/v1/endpoints/practice_cases.py`'s real `/me/practice*`
endpoints. See `app/models/practice_case.py`'s own module docstring for
the full Step 1/2 investigation and scope decisions this mirrors.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel


class PracticeCaseOut(BaseModel):
    """One real, centrally defined practice case (`PRACTICE_CASES`,
    app/models/practice_case.py) merged with the caller's own real
    attempt state, if any."""

    key: str
    case_type: str
    order: int
    title: str
    fake_actor_name: str
    scenario: str
    content_text: str
    allowed_actions: list[str]

    attempted: bool
    action_taken: str | None = None
    notes: str | None = None
    acted_at: datetime | None = None


class PracticeChecklistOut(BaseModel):
    """`GET /me/practice` response -- the caller's own real practice
    progress. Deliberately NOT scored (Scope Decision 1, app/models/
    practice_case.py) -- `attempted_count`/`all_attempted` track real
    completion, never a right/wrong tally."""

    items: list[PracticeCaseOut]
    total: int
    attempted_count: int
    all_attempted: bool


class PracticeActionRequest(BaseModel):
    action: str
    notes: str | None = None


class PracticeActionResponse(BaseModel):
    """Real, honest confirmation -- always includes the same explicit
    reassurance (Step 3's own airtight-boundary ask): this was a
    practice action only, structurally incapable of a real side effect
    (see `app/services/practice_case_service.py`'s own module
    docstring)."""

    success: bool
    case_key: str
    action_taken: str
    all_attempted: bool
    message: str = (
        "This was a practice action only -- no real notification was sent, no real ban was "
        "applied, and no real content was touched."
    )
