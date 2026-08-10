import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.services.cultural_localizer import CulturalLocalizationResult, cultural_localizer_gateway

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/localize-sop", tags=["cultural-localizer"])


class LocalizeSopRequest(BaseModel):
    sop_text: str = Field(
        min_length=10,
        max_length=12000,
        description="The student's original SOP or motivation letter text.",
    )
    target_country: str = Field(
        min_length=2,
        max_length=100,
        description="The country whose academic/cultural expectations to "
        "localize for, e.g. 'USA', 'Germany', 'Japan'.",
    )


class LocalizeSopResponse(BaseModel):
    result: CulturalLocalizationResult


# Path is exactly "/localize-sop" -- same `@router.post("")` +
# `prefix="/localize-sop"` pattern used by every other single-purpose
# POST endpoint since Feature 1.
@router.post("", response_model=LocalizeSopResponse)
async def localize_sop(
    payload: LocalizeSopRequest, current_user: CurrentUser
) -> LocalizeSopResponse:
    # No opportunity/DB lookup here -- same precedent as polisher.py
    # (Feature 7), professor.py (Advanced Feature 1), and
    # devils_advocate.py (Advanced Feature 7): this operates purely on
    # the text supplied, no opportunity/DB context needed or requested.
    try:
        result = await cultural_localizer_gateway.localize_sop(
            payload.sop_text, payload.target_country
        )
    except Exception:
        logger.exception("SOP cultural localization failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="SOP cultural localization is temporarily unavailable.",
        )

    return LocalizeSopResponse(result=result)
