import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.services.uwc_strategist import (
    UwcStrategyResult,
    uwc_strategist_gateway,
)

logger = logging.getLogger(__name__)

# Two path segments in the ask ("POST /uwc-strategist/plan") -- `prefix`
# carries the first segment, `@router.post("/plan")` the second, same
# multi-segment approach as `/test-pathway/plan`.
router = APIRouter(prefix="/uwc-strategist", tags=["uwc-strategist"])


class GenerateUwcStrategyRequest(BaseModel):
    passion_cause: str = Field(
        min_length=1, max_length=255, description='E.g. "Climate Change", "Education Equality".'
    )
    current_activities: list[str] = Field(min_length=1)


class GenerateUwcStrategyResponse(BaseModel):
    result: UwcStrategyResult


# Path is exactly "/uwc-strategist/plan" -- `prefix="/uwc-strategist"` +
# `@router.post("/plan")`.
@router.post("/plan", response_model=GenerateUwcStrategyResponse)
async def generate_uwc_strategy(
    payload: GenerateUwcStrategyRequest, current_user: CurrentUser
) -> GenerateUwcStrategyResponse:
    # No DbSession here -- same posture as every High School feature so
    # far: every input is a literal request-body field.
    try:
        result = await uwc_strategist_gateway.generate_uwc_strategy(
            payload.passion_cause,
            payload.current_activities,
        )
    except Exception:
        logger.exception("UWC strategy generation failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="UWC strategy generation is temporarily unavailable.",
        )

    return GenerateUwcStrategyResponse(result=result)
