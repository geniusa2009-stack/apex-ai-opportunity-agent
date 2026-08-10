import logging
import uuid

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, DbSession
from app.schemas.social import (
    AchievementPostCreateRequest,
    AchievementPostOut,
    CongratsOut,
    ConnectionOut,
    ConnectionRespondRequest,
    DuelChallengeRequest,
    DuelCompleteRequest,
    DuelRespondRequest,
    FriendOut,
    FriendRequestCreate,
    FriendSuggestionOut,
    StudyDuelOut,
)
from app.services import coach_service, social_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/social", tags=["social"])


# ---------------------------------------------------------------------
# Achievements / feed / congrats
# ---------------------------------------------------------------------


@router.post("/achievements", response_model=AchievementPostOut, status_code=status.HTTP_201_CREATED)
async def create_achievement(
    payload: AchievementPostCreateRequest, current_user: CurrentUser, db: DbSession
) -> AchievementPostOut:
    try:
        post = await social_service.create_achievement_post(
            db, current_user.id, payload.achievement_type, payload.raw_description, payload.privacy
        )
    except social_service.CardGenerationError as exc:
        logger.exception("Achievement card generation failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Failed to create achievement post: {exc}"
        ) from exc

    return AchievementPostOut.model_validate(post)


@router.get("/feed", response_model=list[AchievementPostOut])
async def feed(current_user: CurrentUser, db: DbSession) -> list[AchievementPostOut]:
    posts = await social_service.get_feed(db, current_user.id)
    return [AchievementPostOut.model_validate(p) for p in posts]


@router.post("/achievements/{post_id}/congrats", response_model=CongratsOut)
async def congratulate(post_id: uuid.UUID, current_user: CurrentUser, db: DbSession) -> CongratsOut:
    try:
        result = await social_service.congratulate_achievement(db, current_user.id, post_id)
    except social_service.AchievementPostNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Achievement post not found"
        ) from exc
    except social_service.AlreadyCongratulatedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="You've already congratulated this achievement"
        ) from exc

    return CongratsOut(**result)


# ---------------------------------------------------------------------
# Friend connections
# ---------------------------------------------------------------------


@router.post("/friends/request", response_model=ConnectionOut, status_code=status.HTTP_201_CREATED)
async def send_friend_request(
    payload: FriendRequestCreate, current_user: CurrentUser, db: DbSession
) -> ConnectionOut:
    try:
        connection = await social_service.request_friend(db, current_user.id, payload.friend_id)
    except social_service.SelfConnectionError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except social_service.DuplicateConnectionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return ConnectionOut.model_validate(connection)


@router.get("/friends/list", response_model=list[FriendOut])
async def friends_list(current_user: CurrentUser, db: DbSession) -> list[FriendOut]:
    friends = await social_service.get_friends_list(db, current_user.id)
    return [FriendOut.model_validate(u) for u in friends]


@router.get("/friends/suggestions", response_model=list[FriendSuggestionOut])
async def friend_suggestions(current_user: CurrentUser, db: DbSession) -> list[FriendSuggestionOut]:
    """AI Matchmaking & Friends Network. See `social_service.
    get_friend_suggestions` for the shared-course/similar-XP scoring."""
    suggestions = await social_service.get_friend_suggestions(db, current_user.id)
    return [FriendSuggestionOut(**s) for s in suggestions]


# --- Disclosed necessary plumbing: the ask names `POST
# /social/friends/request` and `GET /social/friends/list` but no endpoint
# to accept/decline a pending request. Without one, every connection
# `request_friend` creates would be stuck `pending` forever -- see
# `social_service.respond_to_friend_request`'s docstring. ---
@router.post("/friends/{connection_id}/respond", response_model=ConnectionOut)
async def respond_to_friend_request(
    connection_id: uuid.UUID, payload: ConnectionRespondRequest, current_user: CurrentUser, db: DbSession
) -> ConnectionOut:
    try:
        connection = await social_service.respond_to_friend_request(
            db, current_user.id, connection_id, payload.action
        )
    except social_service.ConnectionNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Friend request not found"
        ) from exc
    except social_service.NotAParticipantError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except social_service.InvalidStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return ConnectionOut.model_validate(connection)


# ---------------------------------------------------------------------
# Study duels
# ---------------------------------------------------------------------


@router.post("/duels/challenge", response_model=StudyDuelOut, status_code=status.HTTP_201_CREATED)
async def challenge_duel(
    payload: DuelChallengeRequest, current_user: CurrentUser, db: DbSession
) -> StudyDuelOut:
    try:
        duel = await social_service.challenge_duel(
            db, current_user.id, payload.opponent_id, payload.duration_minutes, payload.token_wager
        )
    except social_service.SelfDuelError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except coach_service.InsufficientTokensError as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=f"Not enough tokens to wager {payload.token_wager}.",
        ) from exc

    return StudyDuelOut.model_validate(duel)


@router.post("/duels/{duel_id}/respond", response_model=StudyDuelOut)
async def respond_to_duel(
    duel_id: uuid.UUID, payload: DuelRespondRequest, current_user: CurrentUser, db: DbSession
) -> StudyDuelOut:
    try:
        duel = await social_service.manage_duel(db, current_user.id, duel_id, payload.action)
    except social_service.DuelNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Study duel not found") from exc
    except social_service.NotAParticipantError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except social_service.InvalidStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except coach_service.InsufficientTokensError as exc:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="Not enough tokens to match this duel's wager.",
        ) from exc

    return StudyDuelOut.model_validate(duel)


# --- Disclosed necessary plumbing: the ask names `manage_duel` as
# handling "accepting, declining, OR COMPLETING" study duels, but its own
# router section lists only a `respond` endpoint (accept/decline) -- no
# endpoint to actually finish a duel and trigger the wager payout.
# Without one, "completing" would be unreachable dead functionality. See
# `social_service.manage_duel`'s docstring. ---
@router.post("/duels/{duel_id}/complete", response_model=StudyDuelOut)
async def complete_duel(
    duel_id: uuid.UUID, payload: DuelCompleteRequest, current_user: CurrentUser, db: DbSession
) -> StudyDuelOut:
    try:
        duel = await social_service.manage_duel(
            db, current_user.id, duel_id, "complete", payload.winner_id
        )
    except social_service.DuelNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Study duel not found") from exc
    except social_service.NotAParticipantError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except social_service.InvalidStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return StudyDuelOut.model_validate(duel)
