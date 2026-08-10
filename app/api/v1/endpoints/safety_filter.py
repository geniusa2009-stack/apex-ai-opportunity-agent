import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.services.safety_filter import (
    SafetyFilterResult,
    safety_filter_gateway,
)

logger = logging.getLogger(__name__)

# Two path segments in the ask ("POST /safety-filter/analyze") --
# `prefix` carries the first segment, `@router.post("/analyze")` the
# second, same multi-segment approach as `/grade-converter/convert`.
router = APIRouter(prefix="/safety-filter", tags=["safety-filter"])


class AnalyzeSafetyRequest(BaseModel):
    scholarship_name: str = Field(min_length=1, max_length=255)
    city_or_university: str = Field(min_length=1, max_length=255)


class AnalyzeSafetyResponse(BaseModel):
    result: SafetyFilterResult


# Path is exactly "/safety-filter/analyze" -- `prefix="/safety-filter"`
# + `@router.post("/analyze")`.
@router.post("/analyze", response_model=AnalyzeSafetyResponse)
async def analyze_safety_and_funding(
    payload: AnalyzeSafetyRequest, current_user: CurrentUser
) -> AnalyzeSafetyResponse:
    # No DbSession here -- same posture as every High School feature so
    # far: every input is a literal request-body field.
    try:
        result = await safety_filter_gateway.analyze_safety_and_funding(
            payload.scholarship_name,
            payload.city_or_university,
        )
    except Exception:
        logger.exception("Safety and funding analysis failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Safety and funding analysis is temporarily unavailable.",
        )

    return AnalyzeSafetyResponse(result=result)
