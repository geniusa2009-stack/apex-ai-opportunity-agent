import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.services.undergrad_targeter import (
    UndergradTargeterResult,
    undergrad_targeter_gateway,
)

logger = logging.getLogger(__name__)

# Two path segments in the ask ("POST /undergrad-targeter/plan") --
# `prefix` carries the first segment, `@router.post("/plan")` the
# second, same multi-segment approach as `/uwc-strategist/plan`.
router = APIRouter(prefix="/undergrad-targeter", tags=["undergrad-targeter"])


class TargetFutureScholarshipsRequest(BaseModel):
    current_grade: str = Field(min_length=1, max_length=100)
    target_majors: list[str] = Field(min_length=1)
    preferred_regions: list[str] = Field(min_length=1)


class TargetFutureScholarshipsResponse(BaseModel):
    result: UndergradTargeterResult


# Path is exactly "/undergrad-targeter/plan" -- `prefix="/undergrad-targeter"`
# + `@router.post("/plan")`.
@router.post("/plan", response_model=TargetFutureScholarshipsResponse)
async def target_future_scholarships(
    payload: TargetFutureScholarshipsRequest, current_user: CurrentUser
) -> TargetFutureScholarshipsResponse:
    # No DbSession here -- same posture as every High School feature so
    # far: every input is a literal request-body field.
    try:
        result = await undergrad_targeter_gateway.target_future_scholarships(
            payload.current_grade,
            payload.target_majors,
            payload.preferred_regions,
        )
    except Exception:
        logger.exception("Undergrad scholarship targeting failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Undergrad scholarship targeting is temporarily unavailable.",
        )

    return TargetFutureScholarshipsResponse(result=result)
