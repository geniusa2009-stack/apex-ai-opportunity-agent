import logging
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, Field, ValidationError

from app.api.deps import CurrentUser, DbSession, RequireModerationAccess, effective_admin_role, get_websocket_user
from app.models.enums import ModerationAction, ModerationTargetType
from app.core.config import settings
from app.models.chat import ChatMessage as ChatMessageRow, ChatParticipant
from app.schemas.chat import (
    BannedEvent,
    BanStatusOut,
    LeaveRoomResponse,
    MuteRoomResponse,
    ChatMessageOut,
    ChatRoomCreateRequest,
    ChatRoomOut,
    ChatUserOut,
    ErrorEvent,
    IncomingChatMessage,
    JoinPracticeGroupResponse,
    NewMessageEvent,
    PinRoomResponse,
    UnbanResponse,
)
from app.schemas.chat_moderation import ChatMessageReportRequest, ChatMessageReportResponse
from app.services import (
    chat_group_service,
    chat_report_service,
    chat_security,
    chat_service,
    chatbot,
    moderation_log_service,
    notification_service,
)
from app.services.chat_connection_manager import manager, publish_message

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])

# English-Only Gamified Real-time Chat -- this step's own literal
# `ban_user(user_id, 1800)` duration, pulled out as a named constant so
# it's set in exactly one place and reused for both the actual Redis TTL
# (`chat_security.ban_user`'s call below) and the `BannedEvent` payload
# sent to the client (so the frontend's countdown timer is always fed
# the SAME number the backend actually enforced, never a separately
# hardcoded "30 minutes" that could drift from it).
_ARABIC_BAN_DURATION_SECONDS = 1800


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    history: list[ChatMessage] = Field(
        default_factory=list,
        description="Prior turns in this conversation, oldest first. Not "
        "persisted server-side -- there's no chat session storage yet, so "
        "resend the full history each call to maintain context.",
    )


class ChatResponse(BaseModel):
    message: str
    model: str = Field(description="Which model actually answered -- useful for debugging/transparency.")


# Path is exactly "/chat" (no trailing slash) -- `@router.post("")` against
# a router with `prefix="/chat"` resolves to that, matching the literal
# ask ("POST /chat") rather than the "/chat/" a bare `@router.post("/")`
# would produce.
@router.post("", response_model=ChatResponse)
async def send_chat_message(
    payload: ChatRequest, current_user: CurrentUser, db: DbSession
) -> ChatResponse:
    history = [(m.role, m.content) for m in payload.history]

    try:
        reply = await chatbot.chat(payload.message, history, db)
    except Exception:
        logger.exception("Chatbot turn failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Chat is temporarily unavailable.",
        )

    return ChatResponse(message=reply, model=settings.groq_chat_model)


# =======================================================================
# English-Only Gamified Real-time Chat (this step's own actual ask).
#
# Placed in the SAME file, under the SAME `router` (prefix "/chat"), as
# the pre-existing AI chatbot above -- this step's own literal target
# path IS `app/api/v1/endpoints/chat.py`, and that path was already
# taken by an unrelated, already-shipped feature (a stateless AI Q&A
# assistant, `POST /chat`, no database model, no relation whatsoever to
# real-time peer-to-peer messaging). Rather than overwrite/remove that
# existing feature or invent a second router file under a different
# name (which would leave `app/api/v1/endpoints/chat.py` misleadingly
# named for whichever feature DIDN'T end up there), everything below is
# ADDED into this file's existing router, under sub-paths that cannot
# collide with the chatbot's own single `POST /chat` route (`/chat/rooms`,
# `/chat/rooms/{room_id}/messages`, `/chat/ban-status`, `/chat/ws/{room_id}`
# all have path templates strictly longer than, and none share the exact
# same first segment beyond `/chat` as, the bare `/chat` route above --
# confirmed no ambiguity is possible regardless of router registration
# order). The chatbot's own code above is completely untouched.
# =======================================================================


# ---------------------------------------------------------------------
# REST: rooms, message history, ban status
# ---------------------------------------------------------------------


async def _to_room_out(db: DbSession, room, current_user_id: uuid.UUID) -> ChatRoomOut:  # type: ignore[no-untyped-def]
    """Shared helper behind both `POST /chat/rooms` and `GET /chat/rooms`
    -- resolves each room's real `User` rows (see `ChatRoomOut`'s own
    docstring for why this can't just be `ChatRoomOut.model_validate(room)`)
    and builds the response schema explicitly.

    `current_user_id` addition (Auto-Assigned English Practice Groups /
    Pinning): needed to resolve `is_pinned`, which lives on THIS caller's
    own `ChatParticipant` row, not on `ChatRoom` itself -- every call site
    below already has the current user in scope, so this is a plain,
    always-available parameter, not an optional one.
    """
    participants = await chat_service.get_room_participants(db, room.id)
    member_count = len(participants)
    my_participant = await db.get(ChatParticipant, (room.id, current_user_id))
    return ChatRoomOut(
        id=room.id,
        name=room.name,
        is_group=room.is_group,
        created_at=room.created_at,
        participants=[ChatUserOut.model_validate(u) for u in participants],
        is_auto_assigned=room.is_auto_assigned,
        max_members=room.max_members,
        member_count=member_count,
        is_pinned=bool(my_participant and my_participant.is_pinned),
    )


@router.post("/rooms", response_model=ChatRoomOut, status_code=status.HTTP_201_CREATED)
async def create_room(
    payload: ChatRoomCreateRequest, current_user: CurrentUser, db: DbSession
) -> ChatRoomOut:
    """Backs the frontend's "start a DM" action (`is_group=False`) and
    its "Create Group" modal (`is_group=True`). Disclosed necessary
    plumbing -- see `app/services/chat_service.py`'s own module
    docstring: this step's ask specifies the schema and the WebSocket
    flow, not this REST surface, but a chat feature with no way to ever
    CREATE a room in the first place would leave the WebSocket endpoint
    unreachable for any real conversation.
    """
    try:
        if payload.is_group:
            room = await chat_service.create_group_room(
                db, current_user.id, payload.name or "", payload.participant_ids
            )
        else:
            room = await chat_service.get_or_create_dm_room(
                db, current_user.id, payload.participant_ids[0]
            )
    except chat_service.SelfDMError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except chat_service.EmptyGroupError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    return await _to_room_out(db, room, current_user.id)


@router.get("/rooms", response_model=list[ChatRoomOut])
async def list_rooms(current_user: CurrentUser, db: DbSession) -> list[ChatRoomOut]:
    """Backs the frontend sidebar's "Direct Messages"/"Groups" lists --
    the frontend splits this single list into the two sections purely by
    each entry's own `is_group` flag, matching the ask's own "sidebar
    listing active Direct Messages and Groups" wording (one list, two
    client-side buckets) rather than two separate endpoints. As of the
    Auto-Assigned English Practice Groups / Pinning additions, the
    frontend further buckets by `is_auto_assigned` (a third "Auto English
    Practice" section) and `is_pinned` (a "Pinned & Saved" section that
    can pull from either of the other two) -- still one list, now three
    client-side buckets instead of two."""
    rooms = await chat_service.list_my_rooms(db, current_user.id)
    return [await _to_room_out(db, room, current_user.id) for room in rooms]


@router.post("/practice-groups/join", response_model=JoinPracticeGroupResponse, status_code=status.HTTP_200_OK)
async def join_practice_group(current_user: CurrentUser, db: DbSession) -> JoinPracticeGroupResponse:
    """Auto-Assigned English Practice Groups: adds the caller to an
    under-capacity (<30 members) practice room, or creates a fresh one if
    every existing one is full -- see `chat_group_service.
    assign_user_to_practice_group`'s own docstring for the idempotency
    and capacity-check reasoning."""
    room, created = await chat_group_service.assign_user_to_practice_group(db, current_user.id)
    return JoinPracticeGroupResponse(
        room=await _to_room_out(db, room, current_user.id), created_new_group=created
    )


@router.post("/rooms/{room_id}/pin", response_model=PinRoomResponse)
async def pin_room(room_id: uuid.UUID, current_user: CurrentUser, db: DbSession) -> PinRoomResponse:
    """Stars/pins this room for the CALLER only -- see `ChatParticipant.
    is_pinned`'s own model comment for why this is per-participant, not
    room-wide."""
    participant = await chat_group_service.set_pinned(db, room_id, current_user.id, True)
    if participant is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You are not a participant in this chat room."
        )
    return PinRoomResponse(room_id=room_id, is_pinned=True)


@router.delete("/rooms/{room_id}/pin", response_model=PinRoomResponse)
async def unpin_room(room_id: uuid.UUID, current_user: CurrentUser, db: DbSession) -> PinRoomResponse:
    participant = await chat_group_service.set_pinned(db, room_id, current_user.id, False)
    if participant is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You are not a participant in this chat room."
        )
    return PinRoomResponse(room_id=room_id, is_pinned=False)


@router.post("/rooms/{room_id}/mute", response_model=MuteRoomResponse)
async def mute_room(room_id: uuid.UUID, current_user: CurrentUser, db: DbSession) -> MuteRoomResponse:
    """AI-Powered Course Cohorts: mutes this room for the caller only --
    a course cohort/practice group is auto-joined, so mute (rather than
    only leave) lets a member stay a member while silencing it."""
    participant = await chat_group_service.set_muted(db, room_id, current_user.id, True)
    if participant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not a participant of this room")
    return MuteRoomResponse(room_id=room_id, is_muted=participant.is_muted)


@router.delete("/rooms/{room_id}/mute", response_model=MuteRoomResponse)
async def unmute_room(room_id: uuid.UUID, current_user: CurrentUser, db: DbSession) -> MuteRoomResponse:
    participant = await chat_group_service.set_muted(db, room_id, current_user.id, False)
    if participant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not a participant of this room")
    return MuteRoomResponse(room_id=room_id, is_muted=participant.is_muted)


@router.delete("/rooms/{room_id}/leave", response_model=LeaveRoomResponse)
async def leave_room(room_id: uuid.UUID, current_user: CurrentUser, db: DbSession) -> LeaveRoomResponse:
    """Group header's "Leave" action -- removes the caller's own
    membership row. Works for any group (cohort, practice group, or
    user-created) since it's just a `ChatParticipant` delete."""
    left = await chat_group_service.leave_cohort(db, room_id, current_user.id)
    if not left:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not a participant of this room")
    return LeaveRoomResponse(room_id=room_id, left=True)


@router.get("/rooms/{room_id}/messages", response_model=list[ChatMessageOut])
async def room_message_history(
    room_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
    limit: int = Query(default=50, ge=1, le=200),
) -> list[ChatMessageOut]:
    """Backs the frontend's initial chat-window load (existing history
    before the WebSocket connects and starts streaming NEW messages).
    `403`, not `404`, for a room that exists but the caller isn't a
    member of -- same posture as `social_service.NotAParticipantError`'s
    own router mapping, and deliberately does not distinguish "room
    doesn't exist" from "you're not in it" in the response (a 404 here
    would let a caller enumerate which random room UUIDs are real).
    """
    if not await chat_service.is_participant(db, room_id, current_user.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You are not a participant in this chat room."
        )

    messages = await chat_service.get_message_history(db, room_id, limit=limit)
    return [ChatMessageOut.model_validate(m) for m in messages]


@router.post(
    "/rooms/{room_id}/messages/{message_id}/report",
    response_model=ChatMessageReportResponse,
    status_code=status.HTTP_201_CREATED,
)
async def report_message(
    room_id: uuid.UUID,
    message_id: uuid.UUID,
    payload: ChatMessageReportRequest,
    current_user: CurrentUser,
    db: DbSession,
) -> ChatMessageReportResponse:
    """Extend Moderation to Chat Messages -- the genuinely new user-facing
    report action this codebase had no equivalent of before (see
    `app/services/chat_report_service.py`'s own module docstring for the
    real Step 1/2 investigation confirming that, and the real anti-mass-
    report/threshold reasoning).

    Any real participant of the room may report any real message in it
    (403, not 404, for a non-participant -- same posture as
    `room_message_history` immediately above, for the identical "don't
    let a 404 leak which random UUIDs are real rooms" reason). 404 for a
    message that doesn't exist, or that exists in a DIFFERENT room than
    `room_id` names (the path's own `room_id` is trusted as the scope the
    caller already proved membership of; a message_id belonging to some
    other room the caller also happens to be in must not be reportable
    through a mismatched URL).

    400 for a self-report (`SelfReportError`) -- disclosed necessary
    guard, see the service's own docstring. Real 409
    (`DuplicateReportError`) if this caller already has an open report on
    this message -- the real, enforced anti-mass-report rule, not a
    silent idempotent success (a 409 here is honest: the caller's SECOND
    attempt to report the SAME message is a real no-op from the system's
    perspective, and pretending it succeeded again would misrepresent
    what actually happened).
    """
    if not await chat_service.is_participant(db, room_id, current_user.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You are not a participant in this chat room."
        )

    message = await db.get(ChatMessageRow, message_id)
    if message is None or message.room_id != room_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat message not found")

    try:
        case, entry, total = await chat_report_service.create_report(
            db,
            message=message,
            reporter_id=current_user.id,
            reason=payload.reason,
            details=payload.details,
        )
    except chat_report_service.SelfReportError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except chat_report_service.DuplicateReportError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    await db.commit()
    # Same "refresh after commit before reading a server-generated
    # column" posture as `admin_claims.py::claim_item`'s own
    # `db.refresh(item)` call -- `entry.created_at` is a
    # `server_default=func.now()` column; refreshing guarantees the real
    # DB-assigned value is what's returned, regardless of whether the
    # active dialect/driver already eagerly populated it via RETURNING
    # during the flush above.
    await db.refresh(entry)

    return ChatMessageReportResponse(
        report_id=case.id,
        message_id=message.id,
        reason=entry.reason,
        created_at=entry.created_at,
        total_reports=total,
    )


@router.get("/ban-status", response_model=BanStatusOut)
async def ban_status(current_user: CurrentUser) -> BanStatusOut:
    """See `app/services/chat_security.py`'s module docstring for why
    this exists -- restores the frontend's ban countdown UI across a
    page reload, when there's no active WebSocket connection to receive
    a fresh `banned` event from."""
    remaining = await chat_security.get_ban_remaining_seconds(current_user.id)
    return BanStatusOut(is_banned=remaining is not None, remaining_seconds=remaining)


@router.delete("/admin/bans/{user_id}", response_model=UnbanResponse)
async def admin_unban_user(
    user_id: uuid.UUID, current_user: CurrentUser, db: DbSession, _: RequireModerationAccess
) -> UnbanResponse:
    """Moderator-or-higher (Real Tiered Role System step -- lifting a ban
    early is reversible/non-destructive, same tier as the review-queue
    and override actions; see `AdminRole.MODERATOR`'s own docstring).
    Student Dashboard & Admin Control Panel step's own literal "Unban
    Early" action -- "removes the Redis key" for a currently-banned chat
    user, lifting the ban immediately instead of waiting out its
    remaining TTL.

    Routing-precedence check: this path's literal segments
    (`admin`, `bans`, `{user_id}`) share no template with any other
    route already registered under this router's `/chat` prefix --
    `/chat/rooms/{room_id}/messages` and `/chat/ws/{room_id}` both
    diverge at the second segment (`rooms`/`ws` vs. `admin`), so there
    is no ambiguity regardless of route registration order (same
    discipline as every other router in this codebase).

    `RequireAdmin`-gated, same posture as every admin-only route
    elsewhere (`app/api/v1/endpoints/admin_moderation.py`) -- a
    non-admin gets a real `403`, not a silently-ignored request.
    """
    was_banned = await chat_security.unban_user(user_id)

    # Automatic Moderation Activity Log step -- logged regardless of
    # `was_banned` (an admin invoking this action is real and worth
    # recording even if the target turned out not to be banned; the
    # `outcome` field is exactly where that nuance lives, matching this
    # step's own "confirmation the unban succeeded" wording). This is
    # the ONE call site among the four real logged actions with no other
    # DB mutation of its own (`unban_user` is Redis-only) -- the log
    # entry's own `db.commit()` below is a real, deliberate write, not
    # piggybacking on an existing one the way the three video actions in
    # `admin_moderation.py` do. NEXT ADMIN ACTION: see that module's own
    # call sites for the same pattern.
    moderation_log_service.log_action(
        db,
        actor_id=current_user.id,
        actor_role=effective_admin_role(current_user),
        action=ModerationAction.CHAT_UNBAN,
        target_type=ModerationTargetType.USER,
        target_id=user_id,
        outcome={"was_banned": was_banned},
    )
    await db.commit()

    return UnbanResponse(user_id=user_id, was_banned=was_banned)


# ---------------------------------------------------------------------
# WebSocket: WS /api/v1/chat/ws/{room_id} (this step's own literal path)
# ---------------------------------------------------------------------


@router.websocket("/ws/{room_id}")
async def chat_websocket(websocket: WebSocket, room_id: uuid.UUID, db: DbSession) -> None:
    """This step's own five-step logic flow, implemented in order below.
    Every disclosed deviation/addition is called out at its own step;
    see `app/api/deps.py`'s `get_websocket_user` docstring for why
    step 1 (auth) can't use this codebase's usual `HTTPBearer`/
    `CurrentUser` machinery at all.

    `websocket.accept()` happens FIRST, before either the auth or ban
    check -- Starlette's `WebSocket.close(code, reason)` requires the
    connection to have already been accepted to send a custom code +
    reason a browser client can actually read; rejecting a not-yet-
    -accepted connection would surface to the frontend as an
    indistinguishable low-level connection failure instead of a
    meaningful close code, which defeats "reject... with a specific
    close code and message."
    """
    await websocket.accept()

    # --- Step 1: authenticate ---
    user = await get_websocket_user(websocket, db)
    if user is None:
        await websocket.close(code=4401, reason="Missing or invalid authentication token.")
        return

    # --- Step 2: check Redis ban ---
    remaining = await chat_security.get_ban_remaining_seconds(user.id)
    if remaining is not None:
        # Disclosed addition beyond the ask's bare "reject... with a
        # specific close code and message": also sends the SAME typed
        # `banned` JSON event the live-ban flow (step 4 below) sends, so
        # the frontend's `useChatWebSocket` hook has exactly ONE code
        # path to handle "I am banned," whether the ban was just applied
        # this connection or was already active from an earlier one.
        await websocket.send_json(
            BannedEvent(
                reason="You are currently banned from chat.", banned_until_seconds=remaining
            ).model_dump(mode="json")
        )
        await websocket.close(code=4429, reason="You are currently banned from chat.")
        return

    # --- Disclosed necessary addition: room existence + membership ---
    # Not named in the ask's own 5-step flow, but without it, any
    # authenticated, non-banned user could open a WebSocket to ANY
    # `room_id` UUID and both read and post into a conversation they
    # were never added to -- a real authorization gap, not a stylistic
    # nicety, given `ChatParticipant` is exactly the table that's
    # supposed to gate room membership.
    try:
        await chat_service.get_room_or_raise(db, room_id)
    except chat_service.RoomNotFoundError:
        await websocket.send_json(ErrorEvent(reason="Chat room not found.").model_dump(mode="json"))
        await websocket.close(code=4404, reason="Chat room not found.")
        return

    if not await chat_service.is_participant(db, room_id, user.id):
        await websocket.send_json(
            ErrorEvent(reason="You are not a participant in this chat room.").model_dump(mode="json")
        )
        await websocket.close(code=4403, reason="Not a participant in this chat room.")
        return

    manager.connect(room_id, websocket)
    logger.info("chat: user %s joined room %s", user.id, room_id)

    try:
        # --- Step 3: listen for incoming text messages ---
        while True:
            raw_text = await websocket.receive_text()

            try:
                incoming = IncomingChatMessage.model_validate_json(raw_text)
            except ValidationError:
                await websocket.send_json(
                    ErrorEvent(reason="Malformed message payload.").model_dump(mode="json")
                )
                continue

            # --- Step 4: the Arabic filter ---
            if chat_security.contains_arabic(incoming.content):
                await chat_security.ban_user(user.id, _ARABIC_BAN_DURATION_SECONDS)
                logger.info("chat: user %s banned for Arabic content in room %s", user.id, room_id)

                # Repeat-Offender Detection addition (migration 0041) --
                # this automatic ban previously wrote NO durable record
                # at all: the Redis TTL ban self-expires with nothing
                # left behind, so a real "how many times has this user
                # been banned" count silently undercounted every auto-ban
                # that had already expired by the time anyone asked (the
                # confirmed, real gap from this task's own Step 1/2
                # investigation). `actor_id=None`/`actor_role=None` --
                # this is the system itself acting, not a human
                # moderator (see `ModerationLogEntry.actor_role`'s own
                # column docstring for why that's `None`, not a new
                # `AdminRole.SYSTEM` member). `target_type=USER`/
                # `target_id=user.id` is the SAME direct-target
                # convention `CHAT_UNBAN` already uses above -- no
                # `outcome`-JSON extraction is needed to recover the real
                # offender later. This is a standalone commit (not
                # piggybacking on the message-save transaction) since the
                # ban itself is Redis-only, matching `admin_unban_user`'s
                # own "no other DB mutation of its own" posture just
                # above.
                log_entry = moderation_log_service.log_action(
                    db,
                    actor_id=None,
                    actor_role=None,
                    action=ModerationAction.CHAT_AUTO_BAN,
                    target_type=ModerationTargetType.USER,
                    target_id=user.id,
                    outcome={
                        "duration_seconds": _ARABIC_BAN_DURATION_SECONDS,
                        "trigger": "arabic_filter",
                        "room_id": str(room_id),
                    },
                )
                await db.commit()

                # Transparency: Notify Users of Moderation Actions addition
                # -- a real, confirmed gap: this automatic ban notified
                # nobody before this task (the user only ever saw the
                # in-session `BannedEvent` WebSocket close, which is lost
                # once they reconnect). Reuses the exact same fixed reason
                # text already shown in that close event, rather than
                # inventing a second, possibly-inconsistent wording.
                try:
                    await notification_service.create_notification(
                        db,
                        user.id,
                        "WARNING",
                        "You have been banned from chat",
                        "Arabic detected. You are banned from chat for 30 minutes.",
                        payload={
                            "type": "moderation_action",
                            "log_entry_id": str(log_entry.id),
                            "action": ModerationAction.CHAT_AUTO_BAN.value,
                            "appealable": True,
                        },
                    )
                except Exception:
                    logger.exception("Notifications: failed to notify user %s of automatic chat ban", user.id)

                await websocket.send_json(
                    BannedEvent(banned_until_seconds=_ARABIC_BAN_DURATION_SECONDS).model_dump(
                        mode="json"
                    )
                )
                await websocket.close(code=4429, reason="Arabic detected. Banned for 30 minutes.")
                return

            # --- Step 5: save + broadcast valid English messages ---
            message = await chat_service.save_message(db, room_id, user.id, incoming.content)
            # Auto-Assigned English Practice Groups addition: records this
            # participant's activity so the 48-hour inactivity sweep
            # (`chat_group_service.sweep_inactive_participants`) doesn't
            # remove someone who is actively chatting right now. Cheap,
            # harmless no-op for a room this user isn't a participant of
            # (can't happen here -- membership is already verified above)
            # or a non-auto-assigned room (the sweep only ever reads this
            # column for `is_auto_assigned` rooms anyway).
            await chat_group_service.touch_participant_activity(db, room_id, user.id)
            event = NewMessageEvent(
                id=message.id,
                room_id=room_id,
                sender_id=user.id,
                content=message.content,
                created_at=message.created_at,
            )
            # Published via Redis Pub/Sub, not a direct local
            # `manager.broadcast_local` call -- see
            # `app/services/chat_connection_manager.py`'s own module
            # docstring for why this is what actually makes broadcast
            # correct once this backend runs as more than one process.
            await publish_message(room_id, event.model_dump(mode="json"))
    except WebSocketDisconnect:
        logger.info("chat: user %s disconnected from room %s", user.id, room_id)
    finally:
        manager.disconnect(room_id, websocket)
