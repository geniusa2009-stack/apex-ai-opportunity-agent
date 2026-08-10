"""Live 1:1 Video Sessions -- `/live-sessions` router.

REST booking endpoints (request/respond/cancel/complete/mine/ice-config)
plus the WebRTC signaling WebSocket, all in one file -- same "one router
file per feature, REST and WebSocket together" shape as
`app/api/v1/endpoints/chat.py`.

WHY THIS FEATURE EXISTS: offered as one of ten "take the app to a new
level" proposals (open-ended ask), then picked to build first because
every prior AI feature in this codebase is either asynchronous (a Gemini
call returning text/JSON) or, at most, persisted TEXT chat -- nothing
before this let two real people actually see and hear each other live.
This wires a genuine WebRTC video call on top of the booking lifecycle in
`app/services/live_session_service.py`.

WHAT THIS IS NOT: an SFU/MCU media server -- signaling here relays SDP
offers/answers and ICE candidates between exactly two browsers, which
then establish a DIRECT peer-to-peer `RTCPeerConnection` between
themselves. No video/audio bytes ever pass through this backend. See
`settings.live_session_stun_urls`'s own comment for the disclosed
STUN-only (no TURN) scope -- calls between two peers behind restrictive
NATs may fail to connect without a real TURN server, which is not
configured in this sandbox.
"""

import logging
import uuid

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, Field, ValidationError

from app.api.deps import CurrentUser, DbSession, get_websocket_user
from app.core.config import settings
from app.models.user import User
from app.schemas.live_session import (
    IceConfigOut,
    LiveSessionCompleteRequest,
    LiveSessionOut,
    LiveSessionRequestCreate,
    LiveSessionRespondRequest,
    LiveSessionUserSummary,
)
from app.services import live_session_service
from app.services.live_session_connection_manager import manager, publish_signal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/live-sessions", tags=["live-sessions"])


# ---------------------------------------------------------------------
# REST: booking lifecycle
# ---------------------------------------------------------------------


async def _to_out(session, current_user_id: uuid.UUID, db: DbSession) -> LiveSessionOut:  # type: ignore[no-untyped-def]
    """Resolves `other_party` to whichever of `requested_by`/
    `requested_with` is NOT `current_user_id` -- same helper shape as
    `mentorship.py`'s own `_to_out`."""
    other_id = (
        session.requested_with_id
        if session.requested_by_id == current_user_id
        else session.requested_by_id
    )
    other_user = await db.get(User, other_id)
    assert other_user is not None

    return LiveSessionOut(
        id=session.id,
        requested_by_id=session.requested_by_id,
        requested_with_id=session.requested_with_id,
        status=session.status,
        scheduled_at=session.scheduled_at,
        duration_minutes=session.duration_minutes,
        topic=session.topic,
        requested_at=session.requested_at,
        responded_at=session.responded_at,
        rating=session.rating,
        feedback=session.feedback,
        other_party=LiveSessionUserSummary.model_validate(other_user),
    )


@router.post("/request", response_model=LiveSessionOut, status_code=status.HTTP_201_CREATED)
async def request_session(
    payload: LiveSessionRequestCreate, current_user: CurrentUser, db: DbSession
) -> LiveSessionOut:
    try:
        session = await live_session_service.request_session(
            db,
            current_user.id,
            payload.requested_with_id,
            payload.scheduled_at,
            payload.duration_minutes,
            payload.topic,
        )
    except live_session_service.SelfSessionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except live_session_service.CounterpartyNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found") from exc
    except live_session_service.InvalidTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    return await _to_out(session, current_user.id, db)


@router.post("/{session_id}/respond", response_model=LiveSessionOut)
async def respond_to_session(
    session_id: uuid.UUID, payload: LiveSessionRespondRequest, current_user: CurrentUser, db: DbSession
) -> LiveSessionOut:
    try:
        session = await live_session_service.respond_to_session(
            db, session_id, current_user.id, confirm=payload.action == "confirm"
        )
    except live_session_service.LiveSessionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Live session not found") from exc
    except live_session_service.NotTheInvitedPartyError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except live_session_service.InvalidTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return await _to_out(session, current_user.id, db)


@router.post("/{session_id}/cancel", response_model=LiveSessionOut)
async def cancel_session(session_id: uuid.UUID, current_user: CurrentUser, db: DbSession) -> LiveSessionOut:
    try:
        session = await live_session_service.cancel_session(db, session_id, current_user.id)
    except live_session_service.LiveSessionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Live session not found") from exc
    except live_session_service.NotAParticipantError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except live_session_service.InvalidTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return await _to_out(session, current_user.id, db)


@router.post("/{session_id}/complete", response_model=LiveSessionOut)
async def complete_session(
    session_id: uuid.UUID, payload: LiveSessionCompleteRequest, current_user: CurrentUser, db: DbSession
) -> LiveSessionOut:
    try:
        session = await live_session_service.complete_session(
            db, session_id, current_user.id, payload.rating, payload.feedback
        )
    except live_session_service.LiveSessionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Live session not found") from exc
    except live_session_service.NotAParticipantError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except live_session_service.InvalidTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return await _to_out(session, current_user.id, db)


@router.get("/mine", response_model=list[LiveSessionOut])
async def list_my_sessions(current_user: CurrentUser, db: DbSession) -> list[LiveSessionOut]:
    sessions = await live_session_service.list_my_sessions(db, current_user.id)
    return [await _to_out(session, current_user.id, db) for session in sessions]


@router.get("/ice-config", response_model=IceConfigOut)
async def ice_config(current_user: CurrentUser) -> IceConfigOut:
    """The frontend's `useLiveCall` hook fetches this once before calling
    `new RTCPeerConnection(...)`, so the STUN server list lives in one
    place (this backend setting) rather than being hardcoded into the
    frontend bundle. See `settings.live_session_stun_urls`'s own comment
    for the disclosed STUN-only scope."""
    urls = [url.strip() for url in settings.live_session_stun_urls.split(",") if url.strip()]
    return IceConfigOut(ice_servers=[{"urls": url} for url in urls])


# ---------------------------------------------------------------------
# WebSocket: WS /api/v1/live-sessions/ws/{session_id} -- WebRTC signaling
# relay. Not a media server -- see this file's own module docstring.
# ---------------------------------------------------------------------


class _SignalEnvelope(BaseModel):
    """The only shape this relay understands. `type` is opaque to the
    backend -- it never inspects or validates SDP/ICE-candidate contents,
    only relays them to the OTHER participant currently connected.
    `"offer"`/`"answer"` carry a full `RTCSessionDescriptionInit`-shaped
    `data`; `"ice-candidate"` carries an `RTCIceCandidateInit`-shaped
    `data`; `"leave"` carries no meaningful `data` and signals the sender
    is hanging up (the frontend also gets this implicitly from the
    WebSocket closing, but an explicit message lets the OTHER side react
    immediately rather than waiting on a close/timeout)."""

    type: str = Field(min_length=1, max_length=32)
    data: dict = Field(default_factory=dict)


@router.websocket("/ws/{session_id}")
async def live_session_websocket(websocket: WebSocket, session_id: uuid.UUID, db: DbSession) -> None:
    """Same five-step shape as `chat.py`'s own WebSocket handler
    (accept-first, then authenticate, then authorize, then relay):
    `websocket.accept()` happens before any rejection so a custom close
    code/reason is actually readable by the browser client (see that
    file's own docstring for the full "why accept-first" reasoning,
    which applies identically here).
    """
    await websocket.accept()

    user = await get_websocket_user(websocket, db)
    if user is None:
        await websocket.close(code=4401, reason="Missing or invalid authentication token.")
        return

    try:
        await live_session_service.assert_can_join(db, session_id, user.id)
    except live_session_service.LiveSessionNotFoundError:
        await websocket.close(code=4404, reason="Live session not found.")
        return
    except live_session_service.NotAParticipantError:
        await websocket.close(code=4403, reason="You are not part of this live session.")
        return
    except live_session_service.InvalidTransitionError as exc:
        await websocket.close(code=4409, reason=str(exc))
        return

    manager.connect(session_id, websocket)
    logger.info("live_session: user %s joined signaling room %s", user.id, session_id)

    try:
        while True:
            raw_text = await websocket.receive_text()

            try:
                signal = _SignalEnvelope.model_validate_json(raw_text)
            except ValidationError:
                await websocket.send_json({"type": "error", "data": {"reason": "Malformed signal payload."}})
                continue

            # Relayed via Redis Pub/Sub, not a direct local
            # `manager.broadcast_local` call -- same cross-process-
            # correctness reasoning as `chat.py`'s own `publish_message`
            # call (see `live_session_connection_manager.publish_signal`'s
            # own docstring). `sender_id` is stamped on here, server-side
            # -- the receiving peer's `useLiveCall` hook uses it purely to
            # ignore its own echoed-back messages, never to authenticate
            # anything (authorization already happened above, once, at
            # connection time).
            await publish_signal(
                session_id,
                {"type": signal.type, "data": signal.data, "sender_id": str(user.id)},
            )
    except WebSocketDisconnect:
        logger.info("live_session: user %s disconnected from signaling room %s", user.id, session_id)
    finally:
        manager.disconnect(session_id, websocket)
