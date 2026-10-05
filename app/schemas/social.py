from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# `AchievementPost.privacy_level` is written by the ask as an
# exhaustive-looking 3-value list with an explicit default (`public`,
# `friends_only`, `private`) -- `Literal`-validated on the way in, same
# "Literal enforces on write, column stays plain String" pattern as
# `application_tracker.py`'s `_TrackerStatusLiteral`.
_PrivacyLevelLiteral = Literal["public", "friends_only", "private"]

# `StudyDuel.status` is written by the ask as an exhaustive 6-value list
# with no "e.g." -- same `Literal` posture, used here for the
# `respond`/`complete` action inputs' validated outcomes rather than the
# status itself (the status is server-computed, never client-supplied).
_DuelActionLiteral = Literal["accept", "decline"]
_ConnectionActionLiteral = Literal["accept", "decline"]


class AchievementPostCreateRequest(BaseModel):
    """POST /social/achievements request body. `achievement_type` stays
    a plain `str` (not a `Literal`) -- the ask writes it with "e.g."
    (streak_milestone, cv_created, scholarship_secured), non-exhaustive
    wording, same posture as `parent_pitch.primary_concern`."""

    achievement_type: str = Field(min_length=1, max_length=100)
    raw_description: str = Field(min_length=1, max_length=5000)
    privacy: _PrivacyLevelLiteral = "public"


class AchievementPostOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    achievement_type: str
    title: str
    description: str
    privacy_level: str
    congrats_count: int
    shareable_card_data: dict
    created_at: datetime


class CongratsOut(BaseModel):
    """POST /social/achievements/{id}/congrats response."""

    post_id: UUID
    congrats_count: int
    token_awarded: bool
    tokens_balance: int | None = Field(
        default=None,
        description="Null when `token_awarded` is False -- the daily "
        "token cap was already reached, so no balance changed.",
    )


class FriendRequestCreate(BaseModel):
    """POST /social/friends/request request body."""

    friend_id: UUID


class ConnectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    friend_id: UUID
    status: str
    created_at: datetime


class ConnectionRespondRequest(BaseModel):
    """POST /social/friends/{connection_id}/respond request body --
    disclosed necessary addition, see `social_service.respond_to_friend_request`'s
    docstring for why this endpoint has to exist at all."""

    action: _ConnectionActionLiteral


class FriendOut(BaseModel):
    """One entry in `GET /social/friends/list`'s response -- a lightweight
    view of the friend's own `User` row, not the raw `UserConnection`
    edge (which the caller already knows the shape of)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str | None
    full_name: str | None


class PendingFriendRequestOut(BaseModel):
    """One entry in GET /social/friends/requests/pending -- disclosed
    necessary addition, see `social_service.list_pending_incoming_
    requests`'s docstring for why this endpoint has to exist at all.
    Deliberately minimal (no email/phone/gpa/gender) -- same
    minimization posture as `app/schemas/privacy.PublicSearchProfileOut`."""

    connection_id: UUID
    from_user_id: UUID
    from_user_name: str | None
    created_at: datetime


class FriendSuggestionOut(BaseModel):
    """One entry in `GET /social/friends/suggestions` -- see
    `social_service.get_friend_suggestions` for the scoring logic."""

    user_id: UUID
    full_name: str | None
    shared_courses: int
    similar_xp_level: bool
    match_score: int


class DuelChallengeRequest(BaseModel):
    """POST /social/duels/challenge request body."""

    opponent_id: UUID
    duration_minutes: int = Field(default=25, ge=5, le=120)
    token_wager: int = Field(default=10, ge=1)


class DuelRespondRequest(BaseModel):
    """POST /social/duels/{id}/respond request body."""

    action: _DuelActionLiteral


class DuelCompleteRequest(BaseModel):
    """POST /social/duels/{id}/complete request body -- disclosed
    necessary addition, see the router's note on this endpoint and
    `social_service.manage_duel`'s docstring for why it has to exist.
    `winner_id=None` reports a tie."""

    winner_id: UUID | None = None


class StudyDuelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    challenger_id: UUID
    opponent_id: UUID
    duration_minutes: int
    token_wager: int
    status: str
    created_at: datetime
