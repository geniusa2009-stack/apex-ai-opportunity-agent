"""Canned Responses / Templates -- service layer."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canned_response import CannedResponse
from app.models.enums import ModerationTargetType


class CannedResponseNotFoundError(Exception):
    pass


async def list_canned_responses(
    db: AsyncSession, *, applies_to: ModerationTargetType | None = None
) -> list[CannedResponse]:
    """`applies_to=None` (the default, no filter param sent at all)
    returns EVERY template. When a real `applies_to` filter IS given,
    returns both that type's own scoped templates AND every
    general-purpose (`applies_to IS NULL`) template -- a Moderator
    picking a response while dismissing a chat report should see the
    chat-report-specific templates AND the general ones, not just one
    or the other (see `app/models/canned_response.py`'s own docstring
    for why `NULL` means "shown for every item type")."""
    stmt = select(CannedResponse).order_by(CannedResponse.title.asc())
    if applies_to is not None:
        stmt = stmt.where(
            (CannedResponse.applies_to == applies_to) | (CannedResponse.applies_to.is_(None))
        )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def create_canned_response(
    db: AsyncSession, *, title: str, body: str, applies_to: ModerationTargetType | None, created_by: uuid.UUID
) -> CannedResponse:
    row = CannedResponse(title=title, body=body, applies_to=applies_to, created_by=created_by)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def get_canned_response_or_raise(db: AsyncSession, response_id: uuid.UUID) -> CannedResponse:
    row = await db.get(CannedResponse, response_id)
    if row is None:
        raise CannedResponseNotFoundError(str(response_id))
    return row


async def update_canned_response(
    db: AsyncSession,
    response_id: uuid.UUID,
    *,
    title: str | None,
    body: str | None,
    applies_to: ModerationTargetType | None,
    clear_applies_to: bool,
) -> CannedResponse:
    row = await get_canned_response_or_raise(db, response_id)
    if title is not None:
        row.title = title
    if body is not None:
        row.body = body
    if clear_applies_to:
        row.applies_to = None
    elif applies_to is not None:
        row.applies_to = applies_to
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def delete_canned_response(db: AsyncSession, response_id: uuid.UUID) -> None:
    row = await get_canned_response_or_raise(db, response_id)
    await db.delete(row)
    await db.commit()


async def use_canned_response(db: AsyncSession, response_id: uuid.UUID) -> CannedResponse:
    """The real Step 3.3 integration point (see
    `app/models/canned_response.py`'s own module docstring) -- bumps
    `times_used` and returns the current row. Callers (the router) pass
    the returned `body` back to the caller as the text to submit,
    verbatim or edited, on whichever existing action endpoint's own
    `notes` field they're filling; this function has no knowledge of
    which action that ends up being, by design (see that same
    docstring for why)."""
    row = await get_canned_response_or_raise(db, response_id)
    row.times_used += 1
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row
