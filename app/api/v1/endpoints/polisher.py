import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.services.polisher import PolishResult, polisher_gateway

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/polish-text", tags=["polisher"])


class PolishTextRequest(BaseModel):
    raw_text: str = Field(
        min_length=10,
        max_length=8000,
        description="The user's rough draft to refine -- an essay "
        "paragraph, application answer, or email, for example.",
    )


class PolishTextResponse(BaseModel):
    result: PolishResult


# Path is exactly "/polish-text" -- same `@router.post("")` +
# `prefix="/polish-text"` pattern as chat.py/grader.py/planner.py/
# tagger.py, to match the literal ask ("POST /polish-text").
@router.post("", response_model=PolishTextResponse)
async def polish_text(payload: PolishTextRequest, current_user: CurrentUser) -> PolishTextResponse:
    # No opportunity lookup here, unlike the other AI features -- this
    # one operates purely on the text the caller supplies, with no
    # opportunity context needed or requested.
    try:
        result = await polisher_gateway.polish_text(payload.raw_text)
    except Exception:
        logger.exception("Text polishing failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Text polishing is temporarily unavailable.",
        )

    return PolishTextResponse(result=result)
