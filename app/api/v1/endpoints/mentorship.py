"""Hyper-Personalized Platform step: `/mentorship` -- "Actions to
request/approve mentors."

Literally, the ask names two actions ("request", "approve"). This router
adds two disclosed, necessary extras (`reject`, `complete`) so the
feature is actually usable end to end rather than getting requesters
permanently stuck or leaving `MentorshipStatus.COMPLETED` (a real enum
member the ask itself names) unreachable -- see
`app/schemas/features.py`'s `MentorshipRespondRequest`/
`MentorshipCompleteRequest` docstrings for the full reasoning on each.

Direction is fixed: the MENTEE always initiates via `POST
/mentorship/request` naming a `mentor_id`; there is no "mentor offers to
mentee" flow in this pass (the ask's own wording, "request/approve
mentors," reads as request-then-approve, not the reverse -- a future
step could add a symmetric offer flow without touching this one).
"""

import datetime as dt
from uuid import UUID

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from app.api.deps import CurrentUser, DbSession
from app.models.features import MentorshipRelation
from app.models.enums import MentorshipStatus
from app.models.user import User
from app.schemas.features import (
    MentorshipCompleteRequest,
    MentorshipRelationOut,
    MentorshipRequestCreate,
    MentorshipRespondRequest,
    MentorshipUserSummary,
)

router = APIRouter(prefix="/mentorship", tags=["mentorship"])


async def _to_out(relation: MentorshipRelation, current_user_id: UUID, db: DbSession) -> MentorshipRelationOut:
    """Builds a `MentorshipRelationOut`, resolving `other_party` to
    whichever of `mentor`/`mentee` is NOT `current_user_id`. `MentorshipRelation`
    has no ORM `relationship()` to `User` (see that model's own module
    docstring -- same "no speculative relationship objects" posture as
    `Certificate`/`LessonStudyAid`), so the other party's row is fetched
    here explicitly, one extra `db.get`, rather than relying on eager
    loading.
    """
    other_id = relation.mentee_id if relation.mentor_id == current_user_id else relation.mentor_id
    other_user = await db.get(User, other_id)
    # `other_user` should always exist (FK integrity guarantees it while
    # the row exists) -- but a `User` row deleted out from under a
    # mentorship it participates in would leave a dangling reference,
    # same "ON DELETE CASCADE means this can't actually happen" trust
    # already placed in every other FK in this codebase.
    assert other_user is not None

    return MentorshipRelationOut(
        id=relation.id,
        mentor_id=relation.mentor_id,
        mentee_id=relation.mentee_id,
        status=relation.status,
        requested_at=relation.requested_at,
        responded_at=relation.responded_at,
        other_party=MentorshipUserSummary.model_validate(other_user),
    )


@router.post("/request", response_model=MentorshipRelationOut, status_code=status.HTTP_201_CREATED)
async def request_mentorship(
    payload: MentorshipRequestCreate, current_user: CurrentUser, db: DbSession
) -> MentorshipRelationOut:
    if payload.mentor_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot request yourself as a mentor."
        )

    mentor = await db.get(User, payload.mentor_id)
    if mentor is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mentor not found")

    relation = MentorshipRelation(
        mentor_id=payload.mentor_id,
        mentee_id=current_user.id,
        status=MentorshipStatus.PENDING,
    )
    db.add(relation)
    try:
        await db.commit()
    except IntegrityError:
        # `uq_mentorship_pair` -- a relation (of ANY status) already
        # exists between these two users. Same "roll back, surface a
        # clean 409 instead of a raw 500" posture as `bookmarks.py`'s own
        # `IntegrityError` handling, though this case genuinely IS an
        # error (unlike a duplicate bookmark, a duplicate mentorship
        # request isn't a silent no-op -- the requester needs to know a
        # relation already exists, possibly in a state they can't see
        # yet, e.g. still PENDING from the other direction).
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A mentorship relation already exists between you and this mentor.",
        )
    await db.refresh(relation)
    return await _to_out(relation, current_user.id, db)


@router.post("/{relation_id}/respond", response_model=MentorshipRelationOut)
async def respond_to_mentorship(
    relation_id: UUID,
    payload: MentorshipRespondRequest,
    current_user: CurrentUser,
    db: DbSession,
) -> MentorshipRelationOut | Response:
    relation = await db.get(MentorshipRelation, relation_id)
    if relation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mentorship relation not found")

    # Only the MENTOR being requested may approve/reject -- the mentee who
    # sent the request has no "approve my own request" action.
    if relation.mentor_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Only the requested mentor can respond to this request."
        )
    if relation.status != MentorshipStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"This relation is already '{relation.status}', not pending.",
        )

    if payload.action == "reject":
        # See `MentorshipRespondRequest`'s own docstring -- reject DELETES
        # the row rather than storing a `REJECTED` status that doesn't
        # exist on `MentorshipStatus`. Since the row is gone, there is no
        # longer a `MentorshipRelationOut` shape that would honestly
        # describe it (its `status` would have to lie about a real enum
        # member) -- a bare `204 No Content` is returned instead, bypassing
        # `response_model` serialization entirely (FastAPI passes a
        # returned `Response` instance straight through).
        await db.delete(relation)
        await db.commit()
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    relation.status = MentorshipStatus.ACTIVE
    relation.responded_at = dt.datetime.now(dt.timezone.utc)
    await db.commit()
    await db.refresh(relation)
    return await _to_out(relation, current_user.id, db)


@router.post("/{relation_id}/complete", response_model=MentorshipRelationOut)
async def complete_mentorship(
    relation_id: UUID,
    payload: MentorshipCompleteRequest,
    current_user: CurrentUser,
    db: DbSession,
) -> MentorshipRelationOut:
    relation = await db.get(MentorshipRelation, relation_id)
    if relation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mentorship relation not found")

    if current_user.id not in (relation.mentor_id, relation.mentee_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not part of this relation.")
    if relation.status != MentorshipStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"This relation is '{relation.status}', not active.",
        )

    relation.status = MentorshipStatus.COMPLETED
    relation.responded_at = dt.datetime.now(dt.timezone.utc)
    await db.commit()
    await db.refresh(relation)
    return await _to_out(relation, current_user.id, db)


@router.get("/mine", response_model=list[MentorshipRelationOut])
async def list_my_mentorships(current_user: CurrentUser, db: DbSession) -> list[MentorshipRelationOut]:
    """Every relation `current_user` participates in, as EITHER mentor or
    mentee -- the ask's own "request/approve mentors" framing implies a
    user needs to see both directions (their own mentors AND their own
    mentees) from one call, rather than requiring two separate endpoints
    the frontend has to merge itself."""
    result = await db.execute(
        select(MentorshipRelation)
        .where(
            or_(
                MentorshipRelation.mentor_id == current_user.id,
                MentorshipRelation.mentee_id == current_user.id,
            )
        )
        .order_by(MentorshipRelation.requested_at.desc())
    )
    relations = result.scalars().all()
    return [await _to_out(relation, current_user.id, db) for relation in relations]
