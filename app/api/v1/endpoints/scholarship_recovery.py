import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.services.scholarship_recovery import (
    ScholarshipRecoveryResult,
    scholarship_recovery_gateway,
)

logger = logging.getLogger(__name__)

# Two path segments in the ask ("POST /scholarship-recovery/plan") --
# `prefix` carries the first segment, `@router.post("/plan")` the
# second, same multi-segment approach as `/undergrad-targeter/plan`.
#
# Distinct prefix/tag from the existing `appeal_drafter.py`
# ("/appeal/draft", Legendary Feature 9) -- that feature drafts an
# institution-facing appeal letter; this feature generates purely
# internal resilience/planning content for the student. No overlap.
router = APIRouter(prefix="/scholarship-recovery", tags=["scholarship-recovery"])


class GenerateRecoveryPlanRequest(BaseModel):
    scholarship_name: str = Field(min_length=1, max_length=255)
    perceived_weaknesses: list[str] = Field(min_length=1)
    current_grade: str = Field(min_length=1, max_length=100)


class GenerateRecoveryPlanResponse(BaseModel):
    result: ScholarshipRecoveryResult


# Path is exactly "/scholarship-recovery/plan" --
# `prefix="/scholarship-recovery"` + `@router.post("/plan")`.
@router.post("/plan", response_model=GenerateRecoveryPlanResponse)
async def generate_recovery_plan(
    payload: GenerateRecoveryPlanRequest, current_user: CurrentUser
) -> GenerateRecoveryPlanResponse:
    # No DbSession here -- same posture as every High School feature so
    # far: every input is a literal request-body field.
    try:
        result = await scholarship_recovery_gateway.generate_recovery_plan(
            payload.scholarship_name,
            payload.perceived_weaknesses,
            payload.current_grade,
        )
    except Exception:
        logger.exception("Scholarship recovery plan generation failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Scholarship recovery plan generation is temporarily unavailable.",
        )

    return GenerateRecoveryPlanResponse(result=result)
