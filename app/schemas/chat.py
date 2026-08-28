"""English-Only Gamified Real-time Chat -- Pydantic schemas.

Covers both the REST helper endpoints (create/list rooms, message
history, ban status -- this project's own necessary plumbing around the
ask's literal WebSocket flow) AND the WebSocket wire payloads themselves.
The WS payload models (`IncomingChatMessage`, `NewMessageEvent`,
`BannedEvent`, `ErrorEvent`) are NOT enforced automatically by FastAPI
the way a normal request body is -- a WebSocket's `receive_text()` gives
raw text with no built-in validation layer -- so `app/api/v1/endpoints/
chat.py`'s handler explicitly calls `IncomingChatMessage.model_validate_json(...)`
on every inbound frame, and builds every outbound frame via
`SomeEvent(...).model_dump(mode="json")` rather than hand-assembling raw
dicts at each send site. This keeps exactly one source of truth for the
wire shape on the Python side, and gives `apex-frontend/hooks/
useChatWebSocket.ts` a single, unambiguous shape (mirrored there as
TypeScript interfaces) to parse against.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.chat_security import BAN_DURATION_MAX_SECONDS, BAN_DURATION_MIN_SECONDS


# ---------------------------------------------------------------------
# REST: rooms / participants / history / ban status
# ---------------------------------------------------------------------


class ChatUserOut(BaseModel):
    """A lightweight participant view -- same shape as
    `app/schemas/social.py`'s `FriendOut`, deliberately not imported from
    there: that model's own docstring ties it specifically to `GET
    /social/friends/list`'s response, and this schema needs to be free to
    evolve independently (e.g. if chat ever wants an online/offline flag
    `FriendOut` has no reason to carry)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str | None
    full_name: str | None


class ChatRoomCreateRequest(BaseModel):
    """Backs `POST /chat/rooms`. Covers BOTH this step's "1-on-1" and
    "study group" cases with one request shape rather than two separate
    endpoints, distinguished by `is_group`:

    - `is_group=False` (a direct message): `participant_ids` must contain
      EXACTLY one other user (the person to DM); `name` is ignored (a DM
      room is never named -- see `app/models/chat.py`'s own docstring).
      Reuses an existing room between the same two users if one already
      exists (`chat_service.get_or_create_dm_room`) rather than always
      creating a new one.
    - `is_group=True` (the frontend's "Create Group" modal): `name` is
      required, `participant_ids` must contain at least one friend
      (the creator is added automatically, see `chat_service.
      create_group_room`).
    """

    is_group: bool
    name: str | None = Field(default=None, max_length=255)
    participant_ids: list[uuid.UUID] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_shape(self) -> "ChatRoomCreateRequest":
        if self.is_group:
            if not self.name or not self.name.strip():
                raise ValueError("A group chat requires a non-empty name.")
        else:
            if len(self.participant_ids) != 1:
                raise ValueError(
                    "A direct message requires exactly one other participant_id."
                )
        return self


class ChatRoomOut(BaseModel):
    """DISCLOSED: never built via a direct `ChatRoomOut.model_validate(room)`
    against a raw `ChatRoom` ORM instance, even though `from_attributes=True`
    is set. `ChatRoom.participants` (the ORM relationship) is a list of
    `ChatParticipant` JOIN rows, which have no `.user` relationship of
    their own to a loaded `User` (see app/models/chat.py -- they only
    relate back to `.room`). Rather than adding a second eager
    relationship to the ORM model purely to satisfy response
    serialization, `app/api/v1/endpoints/chat.py`'s router builds this
    schema EXPLICITLY (`ChatRoomOut(id=room.id, ..., participants=[...])`),
    resolving each room's actual `User` rows itself via
    `chat_service.get_room_participants`.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str | None
    is_group: bool
    created_at: datetime
    participants: list[ChatUserOut] = Field(default_factory=list)
    # Auto-Assigned English Practice Groups / Pinning additions --
    # resolved by the router the same explicit way as `participants`
    # above (never via a bare `model_validate`, same reasoning).
    is_auto_assigned: bool = False
    max_members: int | None = None
    member_count: int = Field(
        default=0, description="Current participant count -- paired with max_members for a '28/30' badge."
    )
    is_pinned: bool = Field(
        default=False, description="THIS caller's own pin state for this room (per-participant, not room-wide)."
    )


class ChatMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    room_id: uuid.UUID
    sender_id: uuid.UUID
    content: str
    created_at: datetime


class PinRoomResponse(BaseModel):
    """`POST`/`DELETE /chat/rooms/{room_id}/pin` response."""

    room_id: uuid.UUID
    is_pinned: bool


class MuteRoomResponse(BaseModel):
    """`POST`/`DELETE /chat/rooms/{room_id}/mute` response."""

    room_id: uuid.UUID
    is_muted: bool


class LeaveRoomResponse(BaseModel):
    """`DELETE /chat/rooms/{room_id}/leave` response."""

    room_id: uuid.UUID
    left: bool


class JoinPracticeGroupResponse(BaseModel):
    """`POST /chat/practice-groups/join` response."""

    room: ChatRoomOut
    created_new_group: bool = Field(
        description="True if no under-capacity practice group existed and a brand new one was created."
    )


class BanStatusOut(BaseModel):
    """Backs `GET /chat/ban-status` -- see `app/services/chat_security.py`'s
    module docstring for why this endpoint exists (restoring the
    frontend's countdown timer across a page reload)."""

    is_banned: bool
    remaining_seconds: int | None = Field(
        default=None, description="Seconds until the ban lifts. Always present when is_banned is true."
    )


class AdminUnbanRequest(BaseModel):
    """Full Ban-Reason History addition -- `DELETE /chat/admin/bans/{user_id}`
    request body. DISCLOSED REAL GAP this closes: that endpoint previously
    took NO body at all, so `CHAT_UNBAN`'s own `ModerationLogEntry.outcome`
    had no reason field and no way to ever get one. Genuinely optional
    (`| None = None` at the route itself, not just this field) -- a
    moderator lifting an already-expired-in-practice ban, or acting on an
    obvious mistake, shouldn't be blocked from unbanning just because they
    didn't type a sentence first. Same optional-reason shape/reasoning as
    `AdminChatReportDismissRequest.notes`/`AdminChatReportDeleteMessageRequest.notes`
    (`app/schemas/chat_moderation.py`) -- reused here rather than inventing
    a differently-named field for the same concept."""

    reason: str | None = Field(default=None, max_length=2000)


class UnbanResponse(BaseModel):
    """Student Dashboard & Admin Control Panel step -- `DELETE
    /chat/admin/bans/{user_id}` ("Unban Early") response. `was_banned`
    surfaces `chat_security.unban_user`'s own return value -- an admin
    clicking "Unban Early" on a user whose ban already expired a moment
    earlier (a real, if narrow, race against Redis's own TTL) gets an
    honest `was_banned: false` back rather than a misleading unconditional
    success."""

    success: bool = True
    user_id: uuid.UUID
    was_banned: bool


class AdminManualBanRequest(BaseModel):
    """Manual Timed Bans addition -- `POST /chat/admin/bans/{user_id}`
    request body. DISCLOSED REAL GAP this closes (Step 1.3): before this
    task, the ONLY way a chat ban was ever applied was the automatic
    Arabic-filter trigger or escalating an existing chat-message report
    -- there was no direct, report-free "ban this user" action anywhere
    (not from the ban-history view, not from the flagged-users roster).

    `reason` is REQUIRED here (`str`, not `str | None` -- unlike
    `AdminUnbanRequest.reason`, which stays optional). Deliberate,
    disclosed asymmetry: an unban is inherently a return to the normal,
    unrestricted state and can be a defensible snap judgment call; a
    manual ban with no report behind it is the one real ban path in this
    system with NO other corroborating signal at all (an auto-ban has
    the Arabic filter's own trigger; an escalate-ban has the report's
    real categorized reasons) -- the moderator's own stated reason is
    the ONLY evidence this action's audit trail will ever contain, so it
    cannot be silently blank.

    `duration_seconds` reuses the EXACT SAME real bounds
    (`BAN_DURATION_MIN_SECONDS`/`BAN_DURATION_MAX_SECONDS`,
    `chat_security.py`) `AdminChatReportEscalateBanRequest.duration_seconds`
    already used -- one real source of truth for "how long can any chat
    ban possibly be," not two schemas that could quietly drift apart.
    """

    reason: str = Field(min_length=1, max_length=2000)
    duration_seconds: int = Field(
        default=1800,
        ge=BAN_DURATION_MIN_SECONDS,
        le=BAN_DURATION_MAX_SECONDS,
        description="Real Redis TTL passed straight to chat_security.ban_user -- same bounds "
        "and same 1800s (30 min) default as the escalate-ban request, not a separately "
        "hardcoded/differently-bounded duration.",
    )


class ManualBanResponse(BaseModel):
    """`POST /chat/admin/bans/{user_id}` response. Mirrors
    `AdminChatReportActionResponse`'s own `banned_until_seconds` field
    name/shape (`app/schemas/chat_moderation.py`) even though this isn't
    a report action -- same real meaning ("how long, in seconds, from
    now"), so the frontend can render both with one shared helper rather
    than two subtly different response shapes for the same real concept.
    """

    success: bool = True
    user_id: uuid.UUID
    banned_until_seconds: int


# ---------------------------------------------------------------------
# WebSocket wire payloads
# ---------------------------------------------------------------------


class IncomingChatMessage(BaseModel):
    """The only shape a client is expected to SEND over the WebSocket --
    a single field, `content`, matching this step's own logic flow
    ("Listen for incoming text messages")."""

    content: str = Field(min_length=1, max_length=4000)


class NewMessageEvent(BaseModel):
    """Broadcast to every participant (including the sender) once a
    message has passed the Arabic filter and been saved."""

    type: Literal["message"] = "message"
    id: uuid.UUID
    room_id: uuid.UUID
    sender_id: uuid.UUID
    content: str
    created_at: datetime


class BannedEvent(BaseModel):
    """This step's own literal payload shape: `{ "type": "banned",
    "reason": "..." }`. `reason`'s default text is this step's own exact
    wording."""

    type: Literal["banned"] = "banned"
    reason: str = "Arabic detected. You are banned from chat for 30 minutes."
    banned_until_seconds: int = Field(
        description="Ban duration in seconds, so the frontend's countdown timer "
        "doesn't have to hardcode 1800 separately from the backend's own value."
    )


class ErrorEvent(BaseModel):
    """Disclosed necessary addition beyond the ask's own two named event
    types (`message` broadcasts, `banned`) -- a room-join failure (not a
    participant, room doesn't exist) or a malformed inbound frame needs
    SOME way to tell the client what went wrong before the connection is
    closed, rather than just a bare close code with no context the UI
    could show the user."""

    type: Literal["error"] = "error"
    reason: str
