"""Canned Responses / Templates.

See `app/models/canned_response.py`'s own module docstring for the
full Step 1 investigation (no existing template/snippet mechanism
anywhere) and Step 2 design (Owner-manageable, nullable `applies_to`
scoping, reusing `ModerationTargetType`).

STEP 3.3, INTEGRATION DESIGN: `POST /admin/canned-responses/{id}/use`
is the real integration point with the six existing action/notification
flows that each accept a freeform `notes: str | None` (opportunity
reject, chat/guild report dismiss/resolve, appeal resolve, video
override/reject) -- NOT a change to any of those six endpoints'
request schemas. A Moderator-or-higher calls this endpoint AT
SELECTION time (e.g. when they pick a template from a list shown next
to whichever `notes` field they're about to fill), gets back the
template's real `body` text, and submits that text (verbatim or
lightly edited) as the `notes` value on the action call they were
already about to make. This keeps every existing action endpoint's own
contract completely unchanged (no new optional field on six different
schemas across five different files) while still making `times_used`
real, server-tracked usage data -- it only increments on an actual use,
never on merely viewing the list.
"""

import uuid

from fastapi import APIRouter, HTTPException, Query, status

from app.api.deps import CurrentUser, DbSession, RequireModerationAccess, RequireOwner
from app.models.enums import ModerationTargetType
from app.schemas.canned_responses import (
    CannedResponseCreate,
    CannedResponseOut,
    CannedResponseUpdate,
)
from app.services import canned_response_service

router = APIRouter(tags=["admin-canned-responses"])


@router.get("/admin/canned-responses", response_model=list[CannedResponseOut])
async def list_canned_responses(
    db: DbSession,
    _: RequireModerationAccess,
    applies_to: ModerationTargetType | None = Query(default=None),
) -> list[CannedResponseOut]:
    """Moderator-or-higher -- using templates is broader than authoring
    them (see module docstring's Owner-manageable reasoning, same tier
    split `admin_blacklist.py` already established)."""
    rows = await canned_response_service.list_canned_responses(db, applies_to=applies_to)
    return [CannedResponseOut.model_validate(r) for r in rows]


@router.post(
    "/admin/canned-responses",
    response_model=CannedResponseOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_canned_response(
    payload: CannedResponseCreate, current_user: CurrentUser, db: DbSession, _: RequireOwner
) -> CannedResponseOut:
    row = await canned_response_service.create_canned_response(
        db, title=payload.title, body=payload.body, applies_to=payload.applies_to, created_by=current_user.id
    )
    return CannedResponseOut.model_validate(row)


@router.patch("/admin/canned-responses/{response_id}", response_model=CannedResponseOut)
async def update_canned_response(
    response_id: uuid.UUID, payload: CannedResponseUpdate, db: DbSession, _: RequireOwner
) -> CannedResponseOut:
    try:
        row = await canned_response_service.update_canned_response(
            db,
            response_id,
            title=payload.title,
            body=payload.body,
            applies_to=payload.applies_to,
            clear_applies_to=payload.clear_applies_to,
        )
    except canned_response_service.CannedResponseNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Canned response not found") from exc
    return CannedResponseOut.model_validate(row)


@router.delete("/admin/canned-responses/{response_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_canned_response(response_id: uuid.UUID, db: DbSession, _: RequireOwner) -> None:
    try:
        await canned_response_service.delete_canned_response(db, response_id)
    except canned_response_service.CannedResponseNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Canned response not found") from exc


@router.post("/admin/canned-responses/{response_id}/use", response_model=CannedResponseOut)
async def use_canned_response(response_id: uuid.UUID, db: DbSession, _: RequireModerationAccess) -> CannedResponseOut:
    """Moderator-or-higher -- see module docstring for the full
    integration-point reasoning. Real 404 if the template was deleted
    since the caller's own list was fetched (a real, disclosed race a
    stale client-side list can hit)."""
    try:
        row = await canned_response_service.use_canned_response(db, response_id)
    except canned_response_service.CannedResponseNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Canned response not found") from exc
    return CannedResponseOut.model_validate(row)
