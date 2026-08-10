import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.services.test_pathway_planner import (
    TestPathwayResult,
    test_pathway_planner_gateway,
)

logger = logging.getLogger(__name__)

# Two path segments in the ask ("POST /test-pathway/plan") -- `prefix`
# carries the first segment, `@router.post("/plan")` the second, same
# multi-segment approach as `/summer-programs/match`.
router = APIRouter(prefix="/test-pathway", tags=["test-pathway"])


class PlanTestPathwayRequest(BaseModel):
    current_grade: str = Field(
        min_length=1, max_length=100, description='E.g. "9th Grade", "11th Grade".'
    )
    target_country: str = Field(
        min_length=1, max_length=120, description='E.g. "USA", "Germany", "UK".'
    )
    english_level: str = Field(
        min_length=1, max_length=100, description='E.g. "Beginner", "Intermediate", "Advanced".'
    )


class PlanTestPathwayResponse(BaseModel):
    result: TestPathwayResult


# Path is exactly "/test-pathway/plan" -- `prefix="/test-pathway"` +
# `@router.post("/plan")`.
@router.post("/plan", response_model=PlanTestPathwayResponse)
async def plan_test_pathway(
    payload: PlanTestPathwayRequest, current_user: CurrentUser
) -> PlanTestPathwayResponse:
    # No DbSession here -- same posture as extracurriculars.py,
    # competition_scout.py, and summer_programs.py: every input is a
    # literal request-body field.
    try:
        result = await test_pathway_planner_gateway.plan_test_timeline(
            payload.current_grade,
            payload.target_country,
            payload.english_level,
        )
    except Exception:
        logger.exception("Test pathway planning failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Test pathway planning is temporarily unavailable.",
        )

    return PlanTestPathwayResponse(result=result)
