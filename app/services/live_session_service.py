"""Live 1:1 Video Sessions -- booking lifecycle service.

Shared by both `app/api/v1/endpoints/live_sessions.py` (the REST booking
endpoints) and that same file's WebRTC signaling WebSocket handler --
both need the exact same "does this session exist, and is this user one
of its two participants" checks, so that logic lives here once rather
than being duplicated between an HTTP dependency and a WebSocket handler
(which can't use FastAPI's usual `HTTPException`-raising dependency
injection at all -- see `app/api/deps.py`'s `get_websocket_user`
docstring for that same constraint already being disclosed elsewhere).

State machine (see `LiveSessionStatus`'s own docstring, app/models/enums.py,
for the full reasoning on why five states exist):

    REQUESTED --respond(confirm)--> CONFIRMED --complete--> COMPLETED
       |                                |
       +--respond(decline)--> DECLINED  +--cancel--> CANCELLED
       |
       +--cancel--> CANCELLED

Only `CONFIRMED` sessions may be joined over the signaling WebSocket
(`assert_can_join` below) -- an unconfirmed or already-finished session
has no business admitting a live WebRTC connection.
"""

import datetime as dt
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import LiveSessionStatus
from app.models.live_session import LiveSession
from app.models.user import User


class LiveSessionNotFoundError(Exception):
    """No `LiveSession` row exists for the given id."""


class SelfSessionError(Exception):
    """A user tried to book a session with themselves."""


class CounterpartyNotFoundError(Exception):
    """`requested_with_id` doesn't name a real, existing user."""


class NotAParticipantError(Exception):
    """The acting user is neither `requested_by` nor `requested_with` on
    this session."""


class InvalidTransitionError(Exception):
    """The requested action doesn't apply to this session's current
    `status` (e.g. trying to `respond` to a session that's already
    `CONFIRMED`, or `complete` one that's still `REQUESTED`)."""


class NotTheInvitedPartyError(Exception):
    """Only `requested_with` (the person being asked) may `respond`
    (confirm/decline) to a request -- mirrors `mentorship.py`'s own
    "only the requested mentor can respond" rule."""


async def _get_or_raise(db: AsyncSession, session_id: UUID) -> LiveSession:
    session = await db.get(LiveSession, session_id)
    if session is None:
        raise LiveSessionNotFoundError(str(session_id))
    return session


def is_participant(session: LiveSession, user_id: UUID) -> bool:
    """Pure function -- both participant ids already live on the row
    itself, no extra query needed. Shared by the REST layer's
    authorization checks and the WebSocket handler's connection-admission
    check."""
    return user_id in (session.requested_by_id, session.requested_with_id)


async def request_session(
    db: AsyncSession,
    requested_by_id: UUID,
    requested_with_id: UUID,
    scheduled_at: dt.datetime,
    duration_minutes: int,
    topic: str | None,
) -> LiveSession:
    """Books a new session proposal. `scheduled_at` must be strictly in
    the future -- checked here, not at the database layer (see
    `LiveSession.scheduled_at`'s own column comment for why a portable
    CHECK constraint isn't a good fit for a "compare to wall-clock NOW"
    rule)."""
    if requested_by_id == requested_with_id:
        raise SelfSessionError("You cannot book a live session with yourself.")

    counterparty = await db.get(User, requested_with_id)
    if counterparty is None:
        raise CounterpartyNotFoundError(str(requested_with_id))

    now = dt.datetime.now(dt.timezone.utc)
    if scheduled_at <= now:
        raise InvalidTransitionError("scheduled_at must be in the future.")

    session = LiveSession(
        requested_by_id=requested_by_id,
        requested_with_id=requested_with_id,
        status=LiveSessionStatus.REQUESTED.value,
        scheduled_at=scheduled_at,
        duration_minutes=duration_minutes,
        topic=topic,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def respond_to_session(
    db: AsyncSession, session_id: UUID, current_user_id: UUID, *, confirm: bool
) -> LiveSession:
    """Only `requested_with` (the invited party) may confirm/decline, and
    only while the session is still `REQUESTED`."""
    session = await _get_or_raise(db, session_id)

    if session.requested_with_id != current_user_id:
        raise NotTheInvitedPartyError("Only the invited party can respond to this session request.")
    if session.status != LiveSessionStatus.REQUESTED.value:
        raise InvalidTransitionError(f"This session is already '{session.status}', not requested.")

    session.status = LiveSessionStatus.CONFIRMED.value if confirm else LiveSessionStatus.DECLINED.value
    session.responded_at = dt.datetime.now(dt.timezone.utc)
    await db.commit()
    await db.refresh(session)
    return session


async def cancel_session(db: AsyncSession, session_id: UUID, current_user_id: UUID) -> LiveSession:
    """Either participant may cancel, but only while there's still
    something to cancel (`REQUESTED` or `CONFIRMED`) -- a `DECLINED`,
    already-`CANCELLED`, or `COMPLETED` session has nothing left to back
    out of."""
    session = await _get_or_raise(db, session_id)

    if not is_participant(session, current_user_id):
        raise NotAParticipantError("You are not part of this session.")
    if session.status not in (LiveSessionStatus.REQUESTED.value, LiveSessionStatus.CONFIRMED.value):
        raise InvalidTransitionError(f"This session is '{session.status}' and can no longer be cancelled.")

    session.status = LiveSessionStatus.CANCELLED.value
    session.responded_at = dt.datetime.now(dt.timezone.utc)
    await db.commit()
    await db.refresh(session)
    return session


async def complete_session(
    db: AsyncSession,
    session_id: UUID,
    current_user_id: UUID,
    rating: int | None,
    feedback: str | None,
) -> LiveSession:
    """Either participant may mark a `CONFIRMED` session `COMPLETED`,
    once (see this module's own docstring, and `LiveSession`'s, for the
    disclosed single-sided-rating trade-off -- a second `/complete` call
    against an already-`COMPLETED` session is rejected as an invalid
    transition, it does not overwrite the first rating)."""
    session = await _get_or_raise(db, session_id)

    if not is_participant(session, current_user_id):
        raise NotAParticipantError("You are not part of this session.")
    if session.status != LiveSessionStatus.CONFIRMED.value:
        raise InvalidTransitionError(f"This session is '{session.status}', not confirmed.")

    session.status = LiveSessionStatus.COMPLETED.value
    session.responded_at = dt.datetime.now(dt.timezone.utc)
    session.rating = rating
    session.feedback = feedback
    await db.commit()
    await db.refresh(session)
    return session


async def list_my_sessions(db: AsyncSession, user_id: UUID) -> list[LiveSession]:
    """Every session `user_id` participates in, as EITHER `requested_by`
    OR `requested_with` -- same "one call covers both directions" posture
    as `mentorship_service`'s (inline in `mentorship.py`) own
    `GET /mentorship/mine`."""
    result = await db.execute(
        select(LiveSession)
        .where(
            or_(
                LiveSession.requested_by_id == user_id,
                LiveSession.requested_with_id == user_id,
            )
        )
        .order_by(LiveSession.requested_at.desc())
    )
    return list(result.scalars().all())


async def assert_can_join(db: AsyncSession, session_id: UUID, user_id: UUID) -> LiveSession:
    """Gate for the WebRTC signaling WebSocket -- called once, right
    after that connection authenticates. Only a `CONFIRMED` session, and
    only its two named participants, may open a signaling connection --
    an unconfirmed, declined, cancelled, or already-completed session has
    no live call to signal for."""
    session = await _get_or_raise(db, session_id)
    if not is_participant(session, user_id):
        raise NotAParticipantError("You are not part of this session.")
    if session.status != LiveSessionStatus.CONFIRMED.value:
        raise InvalidTransitionError(f"This session is '{session.status}', not confirmed -- nothing to join.")
    return session
