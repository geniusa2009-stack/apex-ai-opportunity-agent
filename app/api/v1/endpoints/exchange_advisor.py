import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.services.exchange_advisor import (
    ExchangeAdvisorResult,
    exchange_advisor_gateway,
)

logger = logging.getLogger(__name__)

# Two path segments in the ask ("POST /exchange-advisor/match") --
# `prefix` carries the first segment, `@router.post("/match")` the
# second, same multi-segment approach as `/competition-scout/match`.
router = APIRouter(prefix="/exchange-advisor", tags=["exchange-advisor"])


class MatchExchangeProgramsRequest(BaseModel):
    target_region: str = Field(
        min_length=1, max_length=120, description='E.g. "USA", "Europe", "Asia".'
    )
    current_grade: str = Field(min_length=1, max_length=100)
    community_involvement_level: str = Field(
        min_length=1, max_length=100, description='E.g. "None", "Moderate", "High".'
    )


class MatchExchangeProgramsResponse(BaseModel):
    result: ExchangeAdvisorResult


# Path is exactly "/exchange-advisor/match" -- `prefix="/exchange-advisor"`
# + `@router.post("/match")`.
@router.post("/match", response_model=MatchExchangeProgramsResponse)
async def match_exchange_programs(
    payload: MatchExchangeProgramsRequest, current_user: CurrentUser
) -> MatchExchangeProgramsResponse:
    # No DbSession here -- same posture as every High School feature so
    # far: every input is a literal request-body field.
    try:
        result = await exchange_advisor_gateway.advise_exchange_programs(
            payload.target_region,
            payload.current_grade,
            payload.community_involvement_level,
        )
    except Exception:
        logger.exception("Exchange program advising failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Exchange program advising is temporarily unavailable.",
        )

    return MatchExchangeProgramsResponse(result=result)
