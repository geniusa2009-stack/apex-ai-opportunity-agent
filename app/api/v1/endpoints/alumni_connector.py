import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.services.safe_alumni_connector import (
    SafeAlumniMessageResult,
    safe_alumni_connector_gateway,
)

logger = logging.getLogger(__name__)

# Two path segments in the ask ("POST /alumni-connector/draft") --
# `prefix` carries the first segment, `@router.post("/draft")` the
# second, same multi-segment approach as `/safety-filter/analyze`.
#
# Distinct prefix/tag from the existing `alumni.py` ("/match-alumni",
# Advanced Feature 8, Step 3.22, DB-backed alumnus matching) -- this
# feature instead drafts an outreach MESSAGE to an alumnus already
# identified, a different concern entirely. No path collision.
router = APIRouter(prefix="/alumni-connector", tags=["alumni-connector"])


class DraftSafeMessageRequest(BaseModel):
    scholarship_name: str = Field(min_length=1, max_length=255)
    alumni_profile_summary: str = Field(
        min_length=1, max_length=1000, description='E.g. "Studying Computer Science at MIT".'
    )
    student_questions: list[str] = Field(min_length=1)


class DraftSafeMessageResponse(BaseModel):
    result: SafeAlumniMessageResult


# Path is exactly "/alumni-connector/draft" -- `prefix="/alumni-connector"`
# + `@router.post("/draft")`.
@router.post("/draft", response_model=DraftSafeMessageResponse)
async def draft_safe_message(
    payload: DraftSafeMessageRequest, current_user: CurrentUser
) -> DraftSafeMessageResponse:
    # No DbSession here -- same posture as every High School feature so
    # far: every input is a literal request-body field.
    try:
        result = await safe_alumni_connector_gateway.draft_safe_message(
            payload.scholarship_name,
            payload.alumni_profile_summary,
            payload.student_questions,
        )
    except Exception:
        logger.exception("Safe alumni message drafting failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Safe alumni message drafting is temporarily unavailable.",
        )

    return DraftSafeMessageResponse(result=result)
