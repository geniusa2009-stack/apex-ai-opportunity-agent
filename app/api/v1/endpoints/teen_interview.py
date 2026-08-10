import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.services.teen_interview_simulator import (
    TeenInterviewSimulationResult,
    teen_interview_simulator_gateway,
)

logger = logging.getLogger(__name__)

# Two path segments in the ask ("POST /teen-interview/simulate") --
# `prefix` carries the first segment, `@router.post("/simulate")` the
# second, same multi-segment approach as `/safety-filter/analyze`.
#
# Distinct prefix/tag from the existing `interview.py` ("/interview",
# Phase 3 general interview question generation) and
# `multi_agent_interview.py` ("/multi-agent", Legendary Feature 1 group
# panel simulation) -- no path collision with either.
router = APIRouter(prefix="/teen-interview", tags=["teen-interview"])


class SimulateTeenInterviewRequest(BaseModel):
    scholarship_name: str = Field(min_length=1, max_length=255)
    student_concerns: list[str] = Field(
        min_length=1, description='E.g. ["Homesickness", "Nervous about speaking English"].'
    )


class SimulateTeenInterviewResponse(BaseModel):
    result: TeenInterviewSimulationResult


# Path is exactly "/teen-interview/simulate" -- `prefix="/teen-interview"`
# + `@router.post("/simulate")`.
@router.post("/simulate", response_model=SimulateTeenInterviewResponse)
async def simulate_interview(
    payload: SimulateTeenInterviewRequest, current_user: CurrentUser
) -> SimulateTeenInterviewResponse:
    # No DbSession here -- same posture as every High School feature so
    # far: every input is a literal request-body field.
    try:
        result = await teen_interview_simulator_gateway.simulate_interview(
            payload.scholarship_name,
            payload.student_concerns,
        )
    except Exception:
        logger.exception("Teen interview simulation failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Teen interview simulation is temporarily unavailable.",
        )

    return SimulateTeenInterviewResponse(result=result)
