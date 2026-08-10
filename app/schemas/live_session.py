"""Pydantic V2 schemas for Live 1:1 Video Sessions
(`app/models/live_session.py`, `app/api/v1/endpoints/live_sessions.py`).
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import LiveSessionStatus

_LiveSessionRespondAction = Literal["confirm", "decline"]


class LiveSessionUserSummary(BaseModel):
    """A lightweight nested view of the OTHER party in a session --
    same "don't re-serialize the raw edge, show a friendly view of the
    related User" posture as `MentorshipUserSummary`
    (app/schemas/features.py) / `FriendOut` (app/schemas/social.py)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str | None
    full_name: str | None


class LiveSessionOut(BaseModel):
    """One live session, from EITHER party's point of view -- the router
    nests whichever of `requested_by`/`requested_with` is the OTHER party
    relative to `current_user`, exactly like `mentorship.py`'s own
    `_to_out` helper, so the frontend never has to compare
    `current_user.id` against the two id fields itself."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    requested_by_id: UUID
    requested_with_id: UUID
    status: LiveSessionStatus
    scheduled_at: datetime
    duration_minutes: int
    topic: str | None
    requested_at: datetime
    responded_at: datetime | None
    rating: int | None
    feedback: str | None
    # Populated by the router, not `from_attributes` off the ORM row
    # directly (`LiveSession` itself has no `relationship()` -- see that
    # model's own docstring).
    other_party: LiveSessionUserSummary


class LiveSessionRequestCreate(BaseModel):
    """POST /live-sessions/request request body."""

    requested_with_id: UUID
    scheduled_at: datetime
    duration_minutes: int = Field(default=30, ge=15, le=120)
    topic: str | None = Field(default=None, max_length=255)


class LiveSessionRespondRequest(BaseModel):
    """POST /live-sessions/{id}/respond request body."""

    action: _LiveSessionRespondAction


class LiveSessionCompleteRequest(BaseModel):
    """POST /live-sessions/{id}/complete request body. Both fields
    optional -- a session can be marked done with no rating at all (see
    `LiveSession`'s own docstring on the disclosed single-sided-rating
    trade-off)."""

    rating: int | None = Field(default=None, ge=1, le=5)
    feedback: str | None = Field(default=None, max_length=2000)


class IceConfigOut(BaseModel):
    """GET /live-sessions/ice-config response -- the ICE server list the
    frontend's `RTCPeerConnection({ iceServers: [...] })` config needs.
    See `settings.live_session_stun_urls`'s own comment for the disclosed
    STUN-only (no TURN) scope of this."""

    ice_servers: list[dict[str, str]]
