import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.services.research_mentor import (
    ResearchOutlineResult,
    research_mentor_gateway,
)

logger = logging.getLogger(__name__)

# Two path segments in the ask ("POST /research-mentor/outline") --
# `prefix` carries the first segment, `@router.post("/outline")` the
# second, same multi-segment approach as `/safety-filter/analyze`.
router = APIRouter(prefix="/research-mentor", tags=["research-mentor"])


class GenerateResearchOutlineRequest(BaseModel):
    topic_idea: str = Field(min_length=1, max_length=500)
    target_field: str = Field(
        min_length=1, max_length=120, description='E.g. "Biology", "Psychology".'
    )


class GenerateResearchOutlineResponse(BaseModel):
    result: ResearchOutlineResult


# Path is exactly "/research-mentor/outline" -- `prefix="/research-mentor"`
# + `@router.post("/outline")`.
@router.post("/outline", response_model=GenerateResearchOutlineResponse)
async def generate_research_outline(
    payload: GenerateResearchOutlineRequest, current_user: CurrentUser
) -> GenerateResearchOutlineResponse:
    # No DbSession here -- same posture as every High School feature so
    # far: every input is a literal request-body field.
    try:
        result = await research_mentor_gateway.generate_research_outline(
            payload.topic_idea,
            payload.target_field,
        )
    except Exception:
        logger.exception("Research outline generation failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Research outline generation is temporarily unavailable.",
        )

    return GenerateResearchOutlineResponse(result=result)
