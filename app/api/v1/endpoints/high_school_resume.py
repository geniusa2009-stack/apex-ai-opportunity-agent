import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.services.high_school_resume import (
    HighSchoolResumeResult,
    high_school_resume_gateway,
)

logger = logging.getLogger(__name__)

# Two path segments in the ask ("POST /high-school-resume/build") --
# `prefix` carries the first segment, `@router.post("/build")` the
# second, same multi-segment approach as `/uwc-strategist/plan`.
router = APIRouter(prefix="/high-school-resume", tags=["high-school-resume"])


class BuildResumeRequest(BaseModel):
    projects: list[str] = Field(default_factory=list)
    clubs: list[str] = Field(default_factory=list)
    awards: list[str] = Field(default_factory=list)
    hobbies: list[str] = Field(default_factory=list)


class BuildResumeResponse(BaseModel):
    result: HighSchoolResumeResult


# Path is exactly "/high-school-resume/build" --
# `prefix="/high-school-resume"` + `@router.post("/build")`.
@router.post("/build", response_model=BuildResumeResponse)
async def build_resume(
    payload: BuildResumeRequest, current_user: CurrentUser
) -> BuildResumeResponse:
    # No DbSession here -- same posture as every High School feature so
    # far: every input is a literal request-body field.
    #
    # Empty-input 422 guard, same established pattern as autofill.py /
    # probability.py / pdf_filler.py / scholar_agent.py's /trigger: if
    # all four lists are empty there is nothing to build a resume from,
    # and calling the AI would just produce hallucinated content out of
    # thin air -- reject before spending the call.
    if not (payload.projects or payload.clubs or payload.awards or payload.hobbies):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one of projects, clubs, awards, or hobbies must be provided.",
        )

    try:
        result = await high_school_resume_gateway.build_resume(
            payload.projects,
            payload.clubs,
            payload.awards,
            payload.hobbies,
        )
    except Exception:
        logger.exception("High school resume building failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Resume building is temporarily unavailable.",
        )

    return BuildResumeResponse(result=result)
