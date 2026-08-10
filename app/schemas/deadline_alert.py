from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class DeadlineAlertOut(BaseModel):
    """Response shape for every /alerts endpoint. No dedicated request
    body schemas are needed for this router -- `POST /alerts/generate/{id}`
    and `GET /alerts/export-ics/{id}` take their target from the URL path
    only, and `PATCH /alerts/{id}/complete` / `DELETE /alerts/{id}` are
    both bodyless per the ask's literal endpoint descriptions."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    tracker_id: UUID
    title: str
    description: str
    scheduled_date: datetime
    is_completed: bool
    created_at: datetime
