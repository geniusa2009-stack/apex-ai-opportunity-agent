import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import CurrentUser, DbSession, require_tokens
from app.schemas.essay_humanizer import EssayHumanizeRequest
from app.services import coach_service
from app.services import essay_humanizer as humanizer_service
from app.services.history_service import save_artifact
from app.services.storage_service import StorageUploadError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/essay-humanizer", tags=["essay-humanizer"])

# Ecosystem Feature 4's explicit example cost for this feature ("Humanizer
# costs 15 tokens").
TOKEN_COST = 15


@router.post(
    "/process",
    response_model=humanizer_service.EssayHumanizationResult,
    dependencies=[Depends(require_tokens("essay_humanizer", TOKEN_COST))],
)
async def process_essay(
    payload: EssayHumanizeRequest, current_user: CurrentUser, db: DbSession
) -> humanizer_service.EssayHumanizationResult:
    # The `require_tokens` dependency above has already deducted
    # `TOKEN_COST` tokens before this function body runs, per the ask's
    # literal instruction to charge BEFORE calling Gemini. If the Gemini
    # call below fails, the student paid for a feature that produced
    # nothing -- refunded here rather than left as a silent loss. See
    # `require_tokens`'s docstring (app/api/deps.py) for why the refund
    # has to happen here and not inside the dependency itself.
    try:
        result = await humanizer_service.analyze_and_humanize(
            payload.essay_text, payload.target_tone
        )
    except humanizer_service.EssayHumanizationError as exc:
        await coach_service.refund_tokens(db, current_user.id, TOKEN_COST, "essay_humanizer")
        logger.exception("Essay humanization failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to analyze/humanize essay: {exc}",
        ) from exc

    # Saving to history is a secondary, bookkeeping step here -- if it
    # fails (e.g. Supabase Storage is down), the primary thing the caller
    # asked for (a humanized essay) has already succeeded above and is
    # still returned; only a warning is logged rather than failing the
    # whole request over a save-side failure. Disclosed judgment call:
    # the ask describes "save...and return" as one flow but doesn't
    # specify what should happen if the save half fails.
    try:
        await save_artifact(
            db,
            current_user.id,
            feature_key="essay_humanizer",
            title=f"Essay Humanization ({payload.target_tone})",
            payload=result.model_dump(),
        )
    except StorageUploadError:
        logger.exception(
            "Failed to save essay_humanizer result as a history artifact for user %s",
            current_user.id,
        )

    return result
