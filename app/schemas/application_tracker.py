from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# Hardcoded to match app.models.enums.TrackerStatus's values exactly --
# not derived from the enum at runtime (Pydantic's `Literal` needs
# statically-known values, and this project's Python version doesn't
# reliably support unpacking a runtime tuple into `Literal[*values]`).
# Same "Literal duplicates an enum's values for static typing" pattern
# already used elsewhere in this codebase's High School tier schemas.
_TrackerStatusLiteral = Literal[
    "interested", "preparing_docs", "applied", "interviewing", "accepted", "rejected"
]


class TrackerCreate(BaseModel):
    """POST /tracker/ request body.

    Application Intelligence step, Part A/K: `opportunity_id` is new and
    optional -- when provided, this tracker is linked to a real catalog
    `Opportunity` (must exist and be published), and `scholarship_name`
    becomes optional (defaults to the opportunity's own real title when
    omitted). Every pre-existing off-catalog call (`opportunity_id`
    omitted, `scholarship_name` required) is unchanged."""

    scholarship_name: str | None = Field(default=None, min_length=1, max_length=500)
    institution: str | None = Field(default=None, max_length=255)
    deadline: datetime | None = None
    opportunity_id: UUID | None = None


class TrackerStatusUpdate(BaseModel):
    """PATCH /tracker/{id}/status request body."""

    new_status: _TrackerStatusLiteral


class TodoToggleUpdate(BaseModel):
    """PATCH /tracker/{id}/todo/{task_id} request body."""

    is_completed: bool


class TodoItemOut(BaseModel):
    task_id: int
    task_name: str
    is_completed: bool


class TrackerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    scholarship_name: str
    institution: str | None
    # Plain `str` here, not `_TrackerStatusLiteral` -- matches
    # `ApplicationTracker.status`'s own plain-String column type (see
    # that column's comment in app/models/application_tracker.py for
    # why). `TrackerStatusUpdate.new_status` above is where the allowed
    # values are actually enforced, on the way IN; this is just an
    # honest reflection of what's stored, on the way out.
    status: str
    deadline: datetime | None
    notes: str | None
    ai_todo_list: list[TodoItemOut]
    created_at: datetime
    # Application Intelligence step additions.
    opportunity_id: UUID | None = None
    submitted_at: datetime | None = None
