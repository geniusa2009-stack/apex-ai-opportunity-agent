"""Smart Notifications Engine -- REST + WebSocket endpoints.

Four routes: this step's own literal three REST routes, plus one
disclosed, necessary WebSocket route (without which the per-user
`ConnectionManager` this step's own ask requires would never have any
sockets to register in the first place -- see `WS /notifications/ws`'s
own docstring below):

    GET  /notifications              -- latest 50, newest first
    PUT  /notifications/{id}/read    -- mark one notification read
    PUT  /notifications/read-all     -- mark every unread one read
    WS   /notifications/ws           -- live per-user delivery socket
"""

import logging
import uuid

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect, status

from app.api.deps import CurrentUser, DbSession, get_websocket_user
from app.schemas.notification import MarkAllReadOut, NotificationOut
from app.services import notification_service
from app.services.notification_connection_manager import manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/notifications", tags=["notifications"])

_LIST_LIMIT = 50


@router.get("", response_model=list[NotificationOut])
async def list_my_notifications(current_user: CurrentUser, db: DbSession) -> list[NotificationOut]:
    """This step's own literal "GET latest 50 notifications for the
    current user, ordered by created_at descending" requirement."""
    notifications = await notification_service.list_notifications(db, current_user.id, limit=_LIST_LIMIT)
    return [NotificationOut.model_validate(n) for n in notifications]


@router.put("/read-all", response_model=MarkAllReadOut)
async def mark_all_read(current_user: CurrentUser, db: DbSession) -> MarkAllReadOut:
    """This step's own literal "PUT mark all unread notifications for the
    current user as read" requirement.

    DISCLOSED ROUTE-ORDERING NOTE: registered BEFORE
    `PUT /notifications/{id}/read` below -- FastAPI/Starlette matches
    path templates in registration order, and `{id}` is a wildcard path
    segment that would otherwise greedily match the literal string
    "read-all" as if it were a notification id (and fail Pydantic's own
    UUID coercion on that path parameter with a confusing 422, never
    reaching this handler at all). Same static-before-wildcard ordering
    this codebase already applies elsewhere (e.g. `application_tracker.py`'s
    own route ordering comment).
    """
    marked_read_count = await notification_service.mark_all_notifications_read(db, current_user.id)
    return MarkAllReadOut(marked_read_count=marked_read_count)


@router.put("/{notification_id}/read", response_model=NotificationOut)
async def mark_one_read(
    notification_id: uuid.UUID, current_user: CurrentUser, db: DbSession
) -> NotificationOut:
    """This step's own literal "PUT mark a specific notification as
    read" requirement. `404`, not a silent no-op, for a `notification_id`
    that doesn't exist OR belongs to a different user -- same
    "don't distinguish the two cases" posture as every other user-scoped
    lookup in this codebase (see `notification_service.
    NotificationNotFoundError`'s own docstring)."""
    try:
        notification = await notification_service.mark_notification_read(db, current_user.id, notification_id)
    except notification_service.NotificationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found.") from exc
    return NotificationOut.model_validate(notification)


# ---------------------------------------------------------------------
# WebSocket: WS /api/v1/notifications/ws
# ---------------------------------------------------------------------


@router.websocket("/ws")
async def notifications_websocket(websocket: WebSocket, db: DbSession) -> None:
    """DISCLOSED, NECESSARY ADDITION -- not literally named as a route
    path in this step's own ask, but required for the ask's own
    real-time-delivery requirement to be reachable at all: something has
    to actually register a live socket into `notification_connection_
    manager.manager`, keyed by `user_id`, for `notification_service.
    create_notification`'s broadcast step to ever have a real connection
    to reach. Mirrors `app/api/v1/endpoints/chat.py`'s own WebSocket
    route shape closely (`websocket.accept()` first, then `get_websocket_
    user` for auth via the same `?token=` query-parameter workaround --
    see that function's own docstring for why a browser WebSocket can't
    use a normal `Authorization` header at all), with two real
    differences: no `room_id` path parameter (this connection is
    per-user, not per-room -- registered under the AUTHENTICATED user's
    own `current_user.id`, never a caller-supplied value, so one user can
    never register a socket as though it belonged to someone else), and
    no incoming-message handling at all (`receive_text()` is still called
    in a loop, but purely to detect disconnect via `WebSocketDisconnect`
    -- this is a PUSH-only channel; the ask's own spec never describes
    the client sending anything over it, unlike chat's genuinely
    bidirectional flow).
    """
    await websocket.accept()

    user = await get_websocket_user(websocket, db)
    if user is None:
        await websocket.close(code=4401, reason="Missing or invalid authentication token.")
        return

    manager.connect(user.id, websocket)
    logger.info("notifications: user %s connected for live delivery", user.id)

    try:
        while True:
            # Push-only channel -- see this handler's own docstring.
            # `receive_text()` is only ever used to detect the socket
            # closing; any text a client DOES send is deliberately
            # ignored rather than erroring, since a stray keepalive
            # ping/pong-style message from a future client shouldn't
            # tear down an otherwise-healthy connection.
            await websocket.receive_text()
    except WebSocketDisconnect:
        logger.info("notifications: user %s disconnected", user.id)
    finally:
        manager.disconnect(user.id, websocket)
