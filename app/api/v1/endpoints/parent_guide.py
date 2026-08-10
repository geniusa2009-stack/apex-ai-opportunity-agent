import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.services.parent_guide import (
    ParentGuideResult,
    parent_guide_gateway,
)

logger = logging.getLogger(__name__)

# Two path segments in the ask ("POST /parent-guide/generate") --
# `prefix` carries the first segment, `@router.post("/generate")` the
# second, same multi-segment approach as `/teacher-guide/generate`.
router = APIRouter(prefix="/parent-guide", tags=["parent-guide"])


class GenerateParentGuideRequest(BaseModel):
    scholarship_name: str = Field(min_length=1, max_length=255)
    target_country: str = Field(min_length=1, max_length=120)
    financial_need_type: str = Field(
        min_length=1, max_length=100, description='E.g. "Full Need-Based Aid", "Partial Scholarship".'
    )


class GenerateParentGuideResponse(BaseModel):
    result: ParentGuideResult


# Path is exactly "/parent-guide/generate" -- `prefix="/parent-guide"` +
# `@router.post("/generate")`.
@router.post("/generate", response_model=GenerateParentGuideResponse)
async def generate_parent_guide(
    payload: GenerateParentGuideRequest, current_user: CurrentUser
) -> GenerateParentGuideResponse:
    # No DbSession here -- same posture as every High School feature so
    # far: every input is a literal request-body field.
    try:
        result = await parent_guide_gateway.generate_parent_guide(
            payload.scholarship_name,
            payload.target_country,
            payload.financial_need_type,
        )
    except Exception:
        logger.exception("Parent guide generation failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Parent guide generation is temporarily unavailable.",
        )

    return GenerateParentGuideResponse(result=result)
