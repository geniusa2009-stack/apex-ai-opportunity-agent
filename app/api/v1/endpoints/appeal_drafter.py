import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.models.user import User
from app.services.appeal_drafter import AppealDraftResult, appeal_drafter_gateway

logger = logging.getLogger(__name__)

# Two path segments in the ask ("POST /appeal/draft") -- `prefix` carries
# the first segment, `@router.post("/draft")` the second, same
# multi-segment approach as `/recommendation/generate`.
router = APIRouter(prefix="/appeal", tags=["appeal-drafter"])


class DraftAppealRequest(BaseModel):
    rejection_text: str = Field(
        min_length=10,
        max_length=8000,
        description="The rejection email/letter text the student received.",
    )


class DraftAppealResponse(BaseModel):
    result: AppealDraftResult


def _build_student_profile(user: User) -> dict:
    return {
        "major": user.major,
        "degree_level": user.degree_level,
        "gpa": user.gpa,
        "graduation_year": user.graduation_year,
        "skills": ", ".join(user.skills) if user.skills else None,
        "target_country": user.target_country,
    }


@router.post("/draft", response_model=DraftAppealResponse)
async def draft_appeal(
    payload: DraftAppealRequest, current_user: CurrentUser
) -> DraftAppealResponse:
    # No DbSession/opportunity lookup here -- unlike quest_planner.py,
    # this feature isn't anchored to a specific opportunity_id at all
    # (the ask's request body is just rejection_text). The rejection
    # text is the primary content; the student's own profile (already
    # available on CurrentUser, no extra query needed) is enrichment for
    # next_steps_advice only -- same "no empty-profile guard needed"
    # reasoning as quest_planner.py.
    student_profile = _build_student_profile(current_user)

    try:
        result = await appeal_drafter_gateway.draft_appeal_letter(
            payload.rejection_text, student_profile
        )
    except Exception:
        logger.exception("Appeal letter drafting failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Appeal letter drafting is temporarily unavailable.",
        )

    return DraftAppealResponse(result=result)
