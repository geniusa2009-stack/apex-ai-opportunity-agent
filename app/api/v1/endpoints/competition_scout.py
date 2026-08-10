import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.services.competition_scout import (
    CompetitionScoutResult,
    competition_scout_gateway,
)

logger = logging.getLogger(__name__)

# Two path segments in the ask ("POST /competition-scout/match") --
# `prefix` carries the first segment, `@router.post("/match")` the
# second, same multi-segment approach as `/extracurricular/generate`.
router = APIRouter(prefix="/competition-scout", tags=["competition-scout"])


class MatchCompetitionsRequest(BaseModel):
    age: int = Field(ge=5, le=25, description="The student's age.")
    major_interest: str = Field(min_length=1, max_length=255)
    current_skill_level: str = Field(
        min_length=1, max_length=100, description='E.g. "Beginner", "Advanced".'
    )


class MatchCompetitionsResponse(BaseModel):
    result: CompetitionScoutResult


# Path is exactly "/competition-scout/match" -- `prefix="/competition-scout"`
# + `@router.post("/match")`.
@router.post("/match", response_model=MatchCompetitionsResponse)
async def match_competitions(
    payload: MatchCompetitionsRequest, current_user: CurrentUser
) -> MatchCompetitionsResponse:
    # No DbSession here -- same posture as extracurriculars.py: every
    # input the service needs (age/major_interest/current_skill_level) is
    # a literal field in the request body, not something to fetch from
    # the database or the current user's stored profile.
    try:
        result = await competition_scout_gateway.scout_competitions(
            payload.age,
            payload.major_interest,
            payload.current_skill_level,
        )
    except Exception:
        logger.exception("Competition scouting failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Competition scouting is temporarily unavailable.",
        )

    return MatchCompetitionsResponse(result=result)
