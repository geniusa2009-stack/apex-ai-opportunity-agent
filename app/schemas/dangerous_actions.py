"""Two-Person Confirmation for Dangerous Actions -- schemas.

`DangerousActionPendingResponse` is the NEW response the four gated
routes themselves now return (`DELETE /admin/videos/{video_id}`,
`POST .../reject-and-purge-media`, `DELETE /admin/chat/reports/
{report_id}/message`, `DELETE /admin/guild/reports/{report_id}/content`)
-- see `app/models/dangerous_action_confirmation.py`'s own module
docstring, STEP 3, for why those routes no longer execute directly.
`DangerousActionConfirmationOut`/`DangerousActionListOut` back
`POST .../approve`, `POST .../override`, and `GET /admin/moderation/
dangerous-actions`.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.models.enums import ModerationAction, ModerationTargetType


class DangerousActionPendingResponse(BaseModel):
    """Returned with `202 Accepted` by all four gated routes (whether the
    caller's own call created a brand-new pending confirmation, or an
    active one already existed and this call idempotently returned it
    unchanged) -- the action has NOT executed yet."""

    confirmation_id: uuid.UUID
    action: ModerationAction
    item_type: ModerationTargetType
    item_id: uuid.UUID
    status: str
    requested_by: uuid.UUID | None
    requested_at: datetime
    message: str = "Awaiting a second Senior Moderator's (or Owner's) approval before this action executes."


class DangerousActionConfirmationOut(BaseModel):
    """Real, current state of one confirmation row -- returned by
    `POST .../approve`, `POST .../override`, and as each item of
    `GET /admin/moderation/dangerous-actions`'s own listing."""

    id: uuid.UUID
    action: ModerationAction
    item_type: ModerationTargetType
    item_id: uuid.UUID
    notes: str | None
    status: str
    is_override: bool
    requested_by: uuid.UUID | None
    requested_by_name: str | None
    requested_at: datetime
    executed_by: uuid.UUID | None
    executed_by_name: str | None
    executed_at: datetime | None


class DangerousActionListOut(BaseModel):
    items: list[DangerousActionConfirmationOut]
    total: int
