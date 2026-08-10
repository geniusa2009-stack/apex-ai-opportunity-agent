from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models.enums import BookmarkStatus
from app.schemas.courses import CourseOut
from app.schemas.opportunity import OpportunityOut


class BookmarkOut(BaseModel):
    """UserBookmark.opportunity/.course are both eager-loaded (lazy=
    "selectin"), so nesting either full object here is free -- no extra
    query.

    DISCLOSED CHANGE (Hyper-Personalized Platform step, polymorphic
    bookmark extension -- see `app/models/bookmark.py`'s own docstring):
    `opportunity` is now `OpportunityOut | None` (was required) and a new
    `course: CourseOut | None` field is added, mirroring the model's own
    "exactly one of the two is set" invariant. Every EXISTING bookmark row
    (all opportunity-typed, created via the unchanged `POST /bookmarks/
    {opportunity_id}` endpoint) still serializes with `course: null` --
    a non-breaking, additive wire-format change for existing API
    consumers, not a behavioral one.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    opportunity: OpportunityOut | None
    course: CourseOut | None
    status: BookmarkStatus
    created_at: datetime
