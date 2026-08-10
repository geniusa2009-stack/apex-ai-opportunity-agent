import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, DbSession
from app.models.opportunity import Opportunity
from app.services.grader import EssayGradeResult, grader_gateway

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/grade-letter", tags=["grader"])


class GradeLetterRequest(BaseModel):
    opportunity_id: UUID
    letter_text: str = Field(
        min_length=50,
        max_length=8000,
        description="The candidate's motivation letter, as plain text.",
    )


class GradeLetterResponse(BaseModel):
    opportunity_id: UUID
    opportunity_title: str
    evaluation: EssayGradeResult


# Path is exactly "/grade-letter" (no trailing slash) -- same
# `@router.post("")` against a `prefix="/grade-letter"` router pattern
# used by chat.py, to match the literal ask ("POST /grade-letter")
# rather than the "/grade-letter/" a bare `@router.post("/")` would give.
@router.post("", response_model=GradeLetterResponse)
async def grade_letter(
    payload: GradeLetterRequest, current_user: CurrentUser, db: DbSession
) -> GradeLetterResponse:
    opportunity = await db.get(Opportunity, payload.opportunity_id)
    if opportunity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")

    opportunity_details = {
        "title": opportunity.title,
        "description": opportunity.description,
        "eligibility_criteria": opportunity.eligibility_criteria,
        "funding_details": opportunity.funding_details,
    }

    try:
        evaluation = await grader_gateway.grade_motivation_letter(
            payload.letter_text, opportunity_details
        )
    except Exception:
        logger.exception(
            "Letter grading failed for user %s, opportunity %s", current_user.id, opportunity.id
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Letter grading is temporarily unavailable.",
        )

    return GradeLetterResponse(
        opportunity_id=opportunity.id,
        opportunity_title=opportunity.title,
        evaluation=evaluation,
    )
