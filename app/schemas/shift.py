"""Volunteer Coverage / Shift Schedule -- Pydantic schemas.

Backs `app/api/v1/endpoints/admin_shifts.py`: Owner-tier CRUD +
Moderator+ team-wide read + Moderator+ `.ics` export.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, model_validator


class ShiftAssignmentCreate(BaseModel):
    volunteer_id: uuid.UUID
    start_at: datetime
    end_at: datetime
    notes: str | None = None

    @model_validator(mode="after")
    def _end_after_start(self) -> "ShiftAssignmentCreate":
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        return self


class ShiftAssignmentUpdate(BaseModel):
    """All fields optional -- a real partial edit (e.g. just nudging
    `end_at` by an hour shouldn't require re-sending `volunteer_id`).
    The `end_at > start_at` invariant is re-checked at the SERVICE layer
    instead of here (see `shift_service.update_shift`), since enforcing
    it here would require the caller to always send both fields
    together even when only one actually changed."""

    volunteer_id: uuid.UUID | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    notes: str | None = None


class ShiftAssignmentOut(BaseModel):
    id: uuid.UUID
    volunteer_id: uuid.UUID
    volunteer_name: str | None
    volunteer_email: str | None
    start_at: datetime
    end_at: datetime
    notes: str | None
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class ShiftAssignmentWriteResponse(BaseModel):
    """Real create/update response -- `shift` is the real, current row;
    `overlapping_shift_ids` lists any OTHER real shift(s) for the SAME
    volunteer whose real `[start_at, end_at)` window intersects this
    one's. Always present (an empty list, not an omitted field) so the
    frontend never has to guess whether the check ran. See
    `app/models/shift.py`'s own module docstring for why this is an
    honest FYI, not a blocking conflict."""

    shift: ShiftAssignmentOut
    overlapping_shift_ids: list[uuid.UUID]


class ShiftListOut(BaseModel):
    items: list[ShiftAssignmentOut]
    total: int
