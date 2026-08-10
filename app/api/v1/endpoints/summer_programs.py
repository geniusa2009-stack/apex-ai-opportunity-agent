import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.services.summer_program_matchmaker import (
    MAX_AGE,
    MIN_AGE,
    InvalidAgeError,
    SummerProgramMatchResult,
    summer_program_matchmaker_gateway,
)

logger = logging.getLogger(__name__)

# Two path segments in the ask ("POST /summer-programs/match") --
# `prefix` carries the first segment, `@router.post("/match")` the
# second, same multi-segment approach as `/competition-scout/match`.
router = APIRouter(prefix="/summer-programs", tags=["summer-programs"])


class MatchSummerProgramsRequest(BaseModel):
    # Bounds enforced here (request-schema level, 422 on violation before
    # the service is ever called) AND again inside the service itself --
    # the ask explicitly calls out "ensure age bounds are applied here as
    # well, e.g., 12-19" for this feature specifically, so both layers
    # use the same MIN_AGE/MAX_AGE constants from the service module
    # rather than hard-coding the numbers twice.
    age: int = Field(ge=MIN_AGE, le=MAX_AGE, description="The student's age (12-19, pre-college).")
    major_interest: str = Field(min_length=1, max_length=255)
    financial_need_level: str = Field(
        min_length=1,
        max_length=100,
        description='E.g. "High - needs full scholarship", "Medium - partial", "Low - self-funded".',
    )


class MatchSummerProgramsResponse(BaseModel):
    result: SummerProgramMatchResult


# Path is exactly "/summer-programs/match" -- `prefix="/summer-programs"`
# + `@router.post("/match")`.
@router.post("/match", response_model=MatchSummerProgramsResponse)
async def match_summer_programs(
    payload: MatchSummerProgramsRequest, current_user: CurrentUser
) -> MatchSummerProgramsResponse:
    # No DbSession here -- same posture as extracurriculars.py and
    # competition_scout.py: every input is a literal request-body field.
    try:
        result = await summer_program_matchmaker_gateway.match_summer_programs(
            payload.age,
            payload.major_interest,
            payload.financial_need_level,
        )
    except InvalidAgeError as exc:
        # Should be unreachable in practice since the request schema
        # already enforces the same bounds, but kept as defense in depth
        # in case this service is ever called from somewhere else.
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except Exception:
        logger.exception("Summer program matchmaking failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Summer program matchmaking is temporarily unavailable.",
        )

    return MatchSummerProgramsResponse(result=result)
