import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.services.culture_shock_evaluator import (
    CultureShockResult,
    culture_shock_evaluator_gateway,
)

logger = logging.getLogger(__name__)

# Two path segments in the ask ("POST /culture-shock/evaluate") --
# `prefix` carries the first segment, `@router.post("/evaluate")` the
# second, same multi-segment approach as `/safety-filter/analyze`.
router = APIRouter(prefix="/culture-shock", tags=["culture-shock"])


class EvaluateCultureShockRequest(BaseModel):
    target_country: str = Field(min_length=1, max_length=120)
    student_traits: list[str] = Field(
        min_length=1, description='E.g. ["Introverted", "Used to strict rules"].'
    )


class EvaluateCultureShockResponse(BaseModel):
    result: CultureShockResult


# Path is exactly "/culture-shock/evaluate" -- `prefix="/culture-shock"`
# + `@router.post("/evaluate")`.
@router.post("/evaluate", response_model=EvaluateCultureShockResponse)
async def evaluate_culture_shock(
    payload: EvaluateCultureShockRequest, current_user: CurrentUser
) -> EvaluateCultureShockResponse:
    # No DbSession here -- same posture as every High School feature so
    # far: every input is a literal request-body field.
    try:
        result = await culture_shock_evaluator_gateway.evaluate_culture_shock(
            payload.target_country,
            payload.student_traits,
        )
    except Exception:
        logger.exception("Culture shock evaluation failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Culture shock evaluation is temporarily unavailable.",
        )

    return EvaluateCultureShockResponse(result=result)
