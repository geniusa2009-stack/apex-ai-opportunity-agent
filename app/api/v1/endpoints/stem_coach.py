import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.services.stem_admission_coach import (
    InconsistentPracticeQuestionError,
    StemPrepPlanResult,
    stem_admission_coach_gateway,
)

logger = logging.getLogger(__name__)

# Two path segments in the ask ("POST /stem-coach/prep") -- `prefix`
# carries the first segment, `@router.post("/prep")` the second, same
# multi-segment approach as `/test-pathway/plan`.
router = APIRouter(prefix="/stem-coach", tags=["stem-coach"])


class GenerateStemPrepPlanRequest(BaseModel):
    weak_subjects: list[str] = Field(
        min_length=1, description='E.g. ["Spatial IQ", "Physics"].'
    )
    # Judgment call, disclosed (same posture as Step 3.36's `age` bounds):
    # the ask says plain "int" with no range. A bare int would accept
    # nonsensical values like 0 or -5. Upper-bounded at 1000 (~2.7 years)
    # as a generous but sane ceiling for exam prep planning.
    study_days_left: int = Field(ge=1, le=1000)


class GenerateStemPrepPlanResponse(BaseModel):
    result: StemPrepPlanResult


# Path is exactly "/stem-coach/prep" -- `prefix="/stem-coach"` +
# `@router.post("/prep")`.
@router.post("/prep", response_model=GenerateStemPrepPlanResponse)
async def generate_stem_prep_plan(
    payload: GenerateStemPrepPlanRequest, current_user: CurrentUser
) -> GenerateStemPrepPlanResponse:
    # No DbSession here -- same posture as every other High School
    # feature so far: every input is a literal request-body field.
    try:
        result = await stem_admission_coach_gateway.generate_stem_prep_plan(
            payload.weak_subjects,
            payload.study_days_left,
        )
    except InconsistentPracticeQuestionError:
        # Distinct logging from the generic 502 below, same convention as
        # pdf_filler.py's/voice_scholar.py's domain-specific exception
        # mapping -- this is a real, if self-consistency-only, quality
        # failure worth being able to grep for separately in logs.
        logger.exception(
            "STEM prep plan generation produced a self-inconsistent practice question for user %s",
            current_user.id,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="STEM prep plan generation produced an invalid practice question. Please try again.",
        )
    except Exception:
        logger.exception("STEM prep plan generation failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="STEM prep plan generation is temporarily unavailable.",
        )

    return GenerateStemPrepPlanResponse(result=result)
