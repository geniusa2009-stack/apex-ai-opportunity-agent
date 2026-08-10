import logging
import uuid

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession
from app.models.user_history import UserHistory
from app.schemas.history import HistoryCreate, HistoryDetail, HistoryOut, HistoryUpdate
from app.schemas.opportunity import PaginatedResponse
from app.services.history_service import save_artifact
from app.services.storage_service import (
    StorageDeleteError,
    StorageDownloadError,
    StorageUploadError,
    storage_service,
)

logger = logging.getLogger(__name__)

# "Standard CRUD endpoints for History" per the ask, beyond the two
# (POST /, GET /{id}) it details step-by-step. GET / (list) and
# PATCH/DELETE /{id} are added to round this out into an actually
# complete CRUD surface -- same disclosed judgment-call posture as every
# other "the ask says 'standard CRUD', here's what that means concretely"
# gap this project has resolved before, modeled directly on
# app/api/v1/endpoints/bookmarks.py's shape (POST + paginated GET) for
# the closest existing precedent of a user-owned-resource router.
router = APIRouter(prefix="/history", tags=["history"])


async def _get_owned_history_or_404(db: DbSession, current_user, history_id: uuid.UUID) -> UserHistory:
    result = await db.execute(
        select(UserHistory).where(
            UserHistory.id == history_id, UserHistory.user_id == current_user.id
        )
    )
    history = result.scalar_one_or_none()
    if history is None:
        # 404, not 403, whether the row doesn't exist at all or exists but
        # belongs to someone else -- same "don't leak existence of other
        # users' rows" posture as every other user-scoped lookup in this
        # codebase (e.g. bookmarks.py, scholar_agent.py's AgentAlert query).
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="History entry not found")
    return history


@router.post("/", response_model=HistoryOut, status_code=status.HTTP_201_CREATED)
async def create_history_entry(
    payload: HistoryCreate, current_user: CurrentUser, db: DbSession
) -> UserHistory:
    """Saves a new artifact via `app.services.history_service.save_artifact`
    -- see that function's docstring for the 3-step flow it performs
    (Postgres metadata, Supabase Storage payload, background embedding).

    Previously implemented inline here; extracted into `history_service.py`
    as part of Ecosystem Feature 3 (AI Detector Safeguard & Essay
    Humanizer), whose ask needed this exact save flow available as a
    plain importable function for a second caller
    (`essay_humanizer.py`'s router). This endpoint's behavior is
    unchanged -- it's the same code, just no longer duplicated.
    """
    try:
        return await save_artifact(
            db,
            current_user.id,
            payload.feature_key,
            payload.title,
            payload.payload,
            payload.is_pinned,
        )
    except StorageUploadError as exc:
        logger.exception("Failed to upload artifact to Storage")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to store artifact payload: {exc}",
        ) from exc


@router.get("/", response_model=PaginatedResponse[HistoryOut])
async def list_history_entries(
    current_user: CurrentUser,
    db: DbSession,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PaginatedResponse[HistoryOut]:
    """Metadata-only listing -- deliberately does not fetch each row's
    Storage payload (see HistoryOut's docstring). Ordered pinned-first,
    then newest-first, so a user's pinned artifacts surface at the top of
    their history rather than getting buried under newer unpinned ones."""
    total = (
        await db.execute(
            select(func.count())
            .select_from(UserHistory)
            .where(UserHistory.user_id == current_user.id)
        )
    ).scalar_one()

    result = await db.execute(
        select(UserHistory)
        .where(UserHistory.user_id == current_user.id)
        .order_by(UserHistory.is_pinned.desc(), UserHistory.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    items = result.scalars().all()

    return PaginatedResponse[HistoryOut](items=items, total=total, limit=limit, offset=offset)


@router.get("/{history_id}", response_model=HistoryDetail)
async def get_history_entry(
    history_id: uuid.UUID, current_user: CurrentUser, db: DbSession
) -> HistoryDetail:
    """Two steps, exactly per the ask: fetch metadata from Postgres, fetch
    the JSON payload from Supabase Storage, return the merged result."""
    history = await _get_owned_history_or_404(db, current_user, history_id)

    try:
        payload = await storage_service.download_json_payload(history.storage_path)
    except StorageDownloadError as exc:
        logger.exception("Failed to download artifact %s from Storage", history_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to retrieve artifact payload: {exc}",
        ) from exc

    return HistoryDetail(**HistoryOut.model_validate(history).model_dump(), payload=payload)


@router.patch("/{history_id}", response_model=HistoryOut)
async def update_history_entry(
    history_id: uuid.UUID, payload: HistoryUpdate, current_user: CurrentUser, db: DbSession
) -> UserHistory:
    """`exclude_unset` -- same reasoning as UserProfileUpdate
    (app/api/v1/endpoints/users.py): a partial PATCH body like
    `{"is_pinned": true}` shouldn't silently clear `title`."""
    history = await _get_owned_history_or_404(db, current_user, history_id)

    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(history, field, value)

    db.add(history)
    await db.commit()
    await db.refresh(history)
    return history


@router.delete("/{history_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_history_entry(
    history_id: uuid.UUID, current_user: CurrentUser, db: DbSession
) -> None:
    """Deletes the Storage payload, then the Postgres row (which cascades
    to the row's StudentMemory, per that model's `ondelete="CASCADE"`).

    A failed Storage delete does NOT block the Postgres delete -- logged
    as a warning (an orphaned Storage object is a recoverable, low-stakes
    failure mode: harmless leftover bytes) rather than leaving a user
    unable to delete their own history entry because of a transient
    Storage-side error. Disclosed judgment call; see the README's Not
    Included list for this step.
    """
    history = await _get_owned_history_or_404(db, current_user, history_id)

    try:
        await storage_service.delete_json_payload(history.storage_path)
    except StorageDeleteError:
        logger.warning(
            "Failed to delete Storage payload for artifact %s; proceeding with "
            "metadata delete anyway",
            history_id,
        )

    await db.delete(history)
    await db.commit()
