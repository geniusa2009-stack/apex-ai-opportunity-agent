import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.services.recommendation_writer import (
    RecommendationLetterResult,
    recommendation_writer_gateway,
)

logger = logging.getLogger(__name__)

# Two path segments in the ask ("POST /recommendation/generate") --
# `prefix` carries the first segment, `@router.post("/generate")` the
# second, same multi-segment approach as `/peer-review/simulate`.
router = APIRouter(prefix="/recommendation", tags=["recommendation-writer"])


class GenerateRecommendationLetterRequest(BaseModel):
    student_name: str = Field(min_length=1, max_length=255)
    referee_name: str = Field(min_length=1, max_length=255)
    referee_title: str = Field(min_length=1, max_length=255)
    relationship_duration: str = Field(
        min_length=1,
        max_length=500,
        description="How long and in what capacity the referee has known "
        "the student, e.g. '2 years, as thesis advisor'.",
    )
    key_achievements: str = Field(min_length=1, max_length=4000)
    target_scholarship: str = Field(min_length=1, max_length=500)


class GenerateRecommendationLetterResponse(BaseModel):
    result: RecommendationLetterResult


@router.post("/generate", response_model=GenerateRecommendationLetterResponse)
async def generate_recommendation_letter(
    payload: GenerateRecommendationLetterRequest, current_user: CurrentUser
) -> GenerateRecommendationLetterResponse:
    # No opportunity/DB lookup here -- same precedent as polisher.py
    # (Feature 7), professor.py (Advanced Feature 1), and
    # cultural_localizer.py (Legendary Feature 2): this operates purely
    # on the fields the caller supplies.
    try:
        result = await recommendation_writer_gateway.generate_recommendation_letter(
            payload.student_name,
            payload.referee_name,
            payload.referee_title,
            payload.relationship_duration,
            payload.key_achievements,
            payload.target_scholarship,
        )
    except Exception:
        logger.exception("Recommendation letter generation failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Recommendation letter generation is temporarily unavailable.",
        )

    return GenerateRecommendationLetterResponse(result=result)
