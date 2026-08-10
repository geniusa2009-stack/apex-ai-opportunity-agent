import logging

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, DbSession
from app.models.parent_pitch import ParentPitch
from app.schemas.parent_pitch import PitchGenerateRequest, PitchOut
from app.services import parent_pitch_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/parent-pitch", tags=["parent-pitch"])


@router.post("/generate", response_model=PitchOut, status_code=status.HTTP_201_CREATED)
async def generate_pitch(
    payload: PitchGenerateRequest, current_user: CurrentUser, db: DbSession
) -> ParentPitch:
    try:
        return await parent_pitch_service.generate_pitch(
            db,
            current_user.id,
            payload.scholarship_name,
            payload.target_country,
            payload.primary_concern,
        )
    except parent_pitch_service.PitchGenerationError as exc:
        logger.exception("Parent pitch generation failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to generate parent pitch: {exc}",
        ) from exc


@router.get("/history", response_model=list[PitchOut])
async def pitch_history(current_user: CurrentUser, db: DbSession) -> list[ParentPitch]:
    return await parent_pitch_service.get_user_pitches(db, current_user.id)
