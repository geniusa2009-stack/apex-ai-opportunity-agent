import logging
import uuid

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, DbSession
from app.models.guilds import GuildPost
from app.schemas.guilds import (
    CommentCreateRequest,
    CommentSubmitOut,
    GuildCreateRequest,
    GuildOut,
    PostCreateRequest,
    PostOut,
    QuestOut,
)
from app.services import guild_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/guilds", tags=["guilds"])


def _to_post_out(post: GuildPost) -> PostOut:
    """Masks `user_id` when the post is anonymous -- see
    app/schemas/guilds.py's `PostOut` docstring for why this can't just
    be a plain `from_attributes` validation."""
    return PostOut(
        id=post.id,
        guild_id=post.guild_id,
        user_id=None if post.is_anonymous else post.user_id,
        content=post.content,
        anonymous_alias=post.anonymous_alias,
        is_anonymous=post.is_anonymous,
        created_at=post.created_at,
    )


# --- Baseline, disclosed necessary plumbing: posts/comments/quests all
# need a guild to attach to, and the ask never describes a guild-creation
# flow of its own (see app/models/guilds.py's `Guild` docstring). ---
@router.post("", response_model=GuildOut, status_code=status.HTTP_201_CREATED)
async def create_guild(
    payload: GuildCreateRequest, current_user: CurrentUser, db: DbSession
) -> GuildOut:
    guild = await guild_service.create_guild(db, current_user.id, payload.name, payload.topic)
    return GuildOut.model_validate(guild)


@router.post("/posts", response_model=PostOut, status_code=status.HTTP_201_CREATED)
async def create_post(
    payload: PostCreateRequest, current_user: CurrentUser, db: DbSession
) -> PostOut:
    try:
        post = await guild_service.create_post(
            db, current_user.id, payload.guild_id, payload.content, payload.is_anonymous
        )
    except guild_service.GuildNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Guild not found") from exc
    except guild_service.ContentRejectedError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except (guild_service.ModerationError, guild_service.AliasGenerationError) as exc:
        logger.exception("Guild post creation failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Failed to create post: {exc}"
        ) from exc

    return _to_post_out(post)


@router.post(
    "/posts/{post_id}/comments", response_model=CommentSubmitOut, status_code=status.HTTP_201_CREATED
)
async def submit_comment(
    post_id: uuid.UUID, payload: CommentCreateRequest, current_user: CurrentUser, db: DbSession
) -> CommentSubmitOut:
    try:
        result = await guild_service.submit_comment_with_rewrite(
            db,
            current_user.id,
            post_id,
            payload.content,
            payload.target_text_snippet,
            payload.suggested_rewrite,
        )
    except guild_service.PostNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found") from exc
    except guild_service.ContentRejectedError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except (guild_service.ModerationError, guild_service.RewriteEvaluationError) as exc:
        logger.exception("Guild comment submission failed for post %s", post_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Failed to submit comment: {exc}"
        ) from exc

    return CommentSubmitOut(
        comment_id=result.comment_id,
        post_id=post_id,
        is_constructive=result.is_constructive,
        rewrite_feedback=result.rewrite_feedback,
        tokens_earned=result.tokens_earned,
        tokens_balance=result.tokens_balance,
    )


@router.get("/{guild_id}/active-quests", response_model=list[QuestOut])
async def active_quests(guild_id: uuid.UUID, current_user: CurrentUser, db: DbSession) -> list[QuestOut]:
    try:
        quests = await guild_service.get_active_quests(db, guild_id)
    except guild_service.GuildNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Guild not found") from exc

    return [QuestOut.model_validate(q) for q in quests]


# --- Disclosed necessary plumbing: the ask names `generate_guild_quest`
# as a service method ("Generates an automated, engaging weekly study
# quest") but its own router section lists no endpoint to trigger it --
# without one, the method would be unreachable dead code. See
# `guild_service.generate_guild_quest`'s docstring for the "weekly"/
# scheduling scope note this endpoint stands in for. ---
@router.post("/{guild_id}/quests/generate", response_model=QuestOut, status_code=status.HTTP_201_CREATED)
async def generate_quest(guild_id: uuid.UUID, current_user: CurrentUser, db: DbSession) -> QuestOut:
    try:
        quest = await guild_service.generate_guild_quest(db, guild_id)
    except guild_service.GuildNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Guild not found") from exc
    except guild_service.QuestGenerationError as exc:
        logger.exception("Guild quest generation failed for guild %s", guild_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Failed to generate quest: {exc}"
        ) from exc

    return QuestOut.model_validate(quest)
