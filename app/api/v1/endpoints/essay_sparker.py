import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.services.essay_sparker import (
    EssaySparkerResult,
    essay_sparker_gateway,
)

logger = logging.getLogger(__name__)

# Two path segments in the ask ("POST /essay-sparker/generate") --
# `prefix` carries the first segment, `@router.post("/generate")` the
# second, same multi-segment approach as `/extracurricular/generate`.
router = APIRouter(prefix="/essay-sparker", tags=["essay-sparker"])


class GenerateEssayIdeasRequest(BaseModel):
    life_events: list[str] = Field(min_length=1)
    core_values: list[str] = Field(min_length=1)
    target_major: str = Field(min_length=1, max_length=255)


class GenerateEssayIdeasResponse(BaseModel):
    result: EssaySparkerResult


# Path is exactly "/essay-sparker/generate" -- `prefix="/essay-sparker"`
# + `@router.post("/generate")`.
@router.post("/generate", response_model=GenerateEssayIdeasResponse)
async def generate_essay_ideas(
    payload: GenerateEssayIdeasRequest, current_user: CurrentUser
) -> GenerateEssayIdeasResponse:
    # No DbSession here -- same posture as every High School feature so
    # far: every input is a literal request-body field.
    try:
        result = await essay_sparker_gateway.generate_essay_ideas(
            payload.life_events,
            payload.core_values,
            payload.target_major,
        )
    except Exception:
        logger.exception("Essay idea sparking failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Essay idea sparking is temporarily unavailable.",
        )

    return GenerateEssayIdeasResponse(result=result)
