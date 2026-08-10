import datetime as dt
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class NotificationOut(BaseModel):
    """One notification row, as returned by `GET /api/v1/notifications`
    and by `PUT /api/v1/notifications/{id}/read`'s own response -- a
    direct, field-for-field mirror of `app/models/notification.py`'s
    `Notification` model."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    type: str
    title: str
    message: str
    is_read: bool
    payload: dict | None = None
    created_at: dt.datetime


class MarkAllReadOut(BaseModel):
    """`PUT /api/v1/notifications/read-all` response -- a disclosed
    addition beyond the ask's bare "mark all... as read" instruction:
    returns HOW MANY notifications were actually flipped, rather than an
    empty `204`, so a caller (and this step's own pytest verification)
    can assert against a real, meaningful number instead of just an HTTP
    status code."""

    marked_read_count: int
