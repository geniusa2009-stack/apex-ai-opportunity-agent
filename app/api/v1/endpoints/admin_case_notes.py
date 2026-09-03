"""Internal Notes on Cases.

See `app/models/case_note.py`'s own module docstring for the full Step
1 investigation (`StudentVideo.admin_notes` is a real but narrower,
single-scalar, decision-bundled precedent -- not a shared, append-only
thread, and not available on the other four item types) and Step 2
design.

STEP 2 DESIGN, ROUTE SHAPE: `POST/GET /admin/moderation/{item_type}/
{item_id}/notes` -- the SAME `{item_type}/{item_id}` path shape the
Claim System (`admin_claims.py`) and Escalation Path
(`admin_escalations.py`) already established, for the identical
reason: a caller adding or listing notes is always acting from the
context of one specific item they're already looking at.
`_CLAIMABLE_MODELS` is imported and reused UNCHANGED from
`admin_claims.py` for the item-existence check -- notes only ever
attach to the same five real claimable/reviewable item kinds.

STEP 2 DESIGN, TIER -- `RequireModerationAccess` (Moderator-or-higher)
on every route here, including delete. Unlike the Escalation Path's
own deliberate raise/resolve tier SPLIT (a real, structural
authority difference between flagging and clearing a flag), adding a
note is not itself a decision with senior-vs-junior weight -- ANY
Moderator-or-higher's context is equally valuable on a shared thread.
Delete is narrower still, gated in-body (mirroring
`admin_claims.release_claim`'s own "claimant or Owner" pattern): only
the note's own author, or an Owner, may delete it -- a Moderator
correcting their OWN mistake (wrong item, typo) needs no escalation,
but letting any Moderator delete ANY other volunteer's note would let
one person quietly erase another's shared context.
"""

import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import aliased

from app.api.deps import CurrentUser, DbSession, RequireModerationAccess, effective_admin_role
from app.api.v1.endpoints.admin_claims import _CLAIMABLE_MODELS
from app.models.enums import AdminRole, ModerationTargetType
from app.models.user import User
from app.schemas.case_notes import CaseNoteCreate, CaseNoteListOut, CaseNoteOut
from app.services import case_note_service

router = APIRouter(tags=["admin-case-notes"])


async def _get_item_or_404(db: DbSession, item_type: ModerationTargetType, item_id: uuid.UUID) -> None:
    model = _CLAIMABLE_MODELS.get(item_type)
    if model is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"'{item_type.value}' does not support internal notes.",
        )
    item = await db.get(model, item_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{item_type.value} not found")


async def _author_name(db: DbSession, author_id: uuid.UUID | None) -> str | None:
    if author_id is None:
        return None
    user = await db.get(User, author_id)
    return user.full_name if user is not None else None


@router.post(
    "/admin/moderation/{item_type}/{item_id}/notes",
    response_model=CaseNoteOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_case_note(
    item_type: ModerationTargetType,
    item_id: uuid.UUID,
    payload: CaseNoteCreate,
    current_user: CurrentUser,
    db: DbSession,
    _: RequireModerationAccess,
) -> CaseNoteOut:
    await _get_item_or_404(db, item_type, item_id)

    note = await case_note_service.add_note(
        db, item_type=item_type, item_id=item_id, body=payload.body, author_id=current_user.id
    )
    return CaseNoteOut(
        id=note.id,
        item_type=note.item_type,
        item_id=note.item_id,
        body=note.body,
        author_id=note.author_id,
        author_name=current_user.full_name,
        created_at=note.created_at,
    )


@router.get(
    "/admin/moderation/{item_type}/{item_id}/notes",
    response_model=CaseNoteListOut,
)
async def list_case_notes(
    item_type: ModerationTargetType,
    item_id: uuid.UUID,
    db: DbSession,
    _: RequireModerationAccess,
) -> CaseNoteListOut:
    await _get_item_or_404(db, item_type, item_id)

    notes, total = await case_note_service.list_notes(db, item_type, item_id)

    Author = aliased(User)
    names: dict[uuid.UUID, str | None] = {}
    author_ids = {n.author_id for n in notes if n.author_id is not None}
    if author_ids:
        result = await db.execute(select(Author.id, Author.full_name).where(Author.id.in_(author_ids)))
        names = dict(result.all())

    items = [
        CaseNoteOut(
            id=n.id,
            item_type=n.item_type,
            item_id=n.item_id,
            body=n.body,
            author_id=n.author_id,
            author_name=names.get(n.author_id) if n.author_id is not None else None,
            created_at=n.created_at,
        )
        for n in notes
    ]
    return CaseNoteListOut(items=items, total=total)


@router.delete(
    "/admin/moderation/{item_type}/{item_id}/notes/{note_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_case_note(
    item_type: ModerationTargetType,
    item_id: uuid.UUID,
    note_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
    _: RequireModerationAccess,
) -> None:
    """See module docstring: only the note's own author, or an Owner,
    may delete it."""
    try:
        note = await case_note_service.get_note_or_raise(db, note_id)
    except case_note_service.CaseNoteNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Note not found")

    if note.item_type != item_type or note.item_id != item_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Note not found")

    if note.author_id != current_user.id and effective_admin_role(current_user).rank < AdminRole.OWNER.rank:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the note's own author or an Owner may delete it.",
        )

    await case_note_service.delete_note(db, note_id)
