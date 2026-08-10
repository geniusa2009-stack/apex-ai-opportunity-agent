import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.services.extracurricular_architect import (
    ExtracurricularRoadmapResult,
    extracurricular_architect_gateway,
)

logger = logging.getLogger(__name__)

# Two path segments in the ask ("POST /extracurricular/generate") --
# `prefix` carries the first segment, `@router.post("/generate")` the
# second, same multi-segment approach as `/recommendation/generate`.
router = APIRouter(prefix="/extracurricular", tags=["extracurricular-architect"])


class GenerateExtracurricularRoadmapRequest(BaseModel):
    interests: list[str] = Field(min_length=1, description="The student's stated interests.")
    grade_level: str = Field(min_length=1, max_length=100, description="E.g. '10th grade', 'Year 11'.")
    target_major: str = Field(min_length=1, max_length=255)
    available_hours_per_week: int = Field(ge=1, le=80)


class GenerateExtracurricularRoadmapResponse(BaseModel):
    result: ExtracurricularRoadmapResult


# Path is exactly "/extracurricular/generate" -- `prefix="/extracurricular"`
# + `@router.post("/generate")`.
@router.post("/generate", response_model=GenerateExtracurricularRoadmapResponse)
async def generate_extracurricular_roadmap(
    payload: GenerateExtracurricularRoadmapRequest, current_user: CurrentUser
) -> GenerateExtracurricularRoadmapResponse:
    # No DbSession here -- the ask says "if applicable," and it isn't:
    # every input the service needs (interests/grade_level/target_major/
    # available_hours_per_week) is a literal field in the request body,
    # not something to fetch from the database or the current user's
    # stored profile. Same posture as devils_advocate.py/
    # cultural_localizer.py/peer_review.py: auth-gated, but no DB lookup.
    try:
        result = await extracurricular_architect_gateway.generate_extracurricular_roadmap(
            payload.interests,
            payload.grade_level,
            payload.target_major,
            payload.available_hours_per_week,
        )
    except Exception:
        logger.exception("Extracurricular roadmap generation failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Extracurricular roadmap generation is temporarily unavailable.",
        )

    return GenerateExtracurricularRoadmapResponse(result=result)
