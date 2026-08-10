import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.api.deps import CurrentUser, DbSession
from app.models.opportunity import Opportunity
from app.models.user import User
from app.services.probability_engine import ProbabilityResult, probability_engine_gateway

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/calculate-probability", tags=["probability"])


class CalculateProbabilityRequest(BaseModel):
    opportunity_id: UUID


class CalculateProbabilityResponse(BaseModel):
    opportunity_id: UUID
    opportunity_title: str
    result: ProbabilityResult


def _build_student_profile(user: User) -> dict:
    # No `language_test_scores` field exists on `User` -- the ask lists it
    # as one of the four representative profile dimensions to analyze,
    # but Step 3.20's profile extension didn't add it (only
    # phone/degree_level/major/gpa/graduation_year/skills). The AI is
    # instructed to treat a missing data point as a genuine gap/weakness
    # to flag rather than assume a value, so this doesn't break the
    # feature -- it just means language test scores never factor in until
    # a future step adds somewhere to store them. See the README note.
    return {
        "major": user.major,
        "degree_level": user.degree_level,
        "gpa": user.gpa,
        "graduation_year": user.graduation_year,
        "skills": ", ".join(user.skills) if user.skills else None,
    }


# Path is exactly "/calculate-probability" -- same `@router.post("")` +
# `prefix="/calculate-probability"` pattern used by every other
# single-purpose POST endpoint since Feature 1.
@router.post("", response_model=CalculateProbabilityResponse)
async def calculate_probability(
    payload: CalculateProbabilityRequest, current_user: CurrentUser, db: DbSession
) -> CalculateProbabilityResponse:
    opportunity = await db.get(Opportunity, payload.opportunity_id)
    if opportunity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")

    student_profile = _build_student_profile(current_user)
    if not any(student_profile.values()):
        # Same posture as autofill.py (Step 3.20): an all-empty profile
        # can't produce a meaningful probability estimate, so this is
        # more honest than sending an empty prompt to Gemini and getting
        # back a low score that looks like a real assessment but isn't.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No profile information is set yet. Update your profile "
            "via PATCH /api/v1/users/me first.",
        )

    opportunity_details = {
        "title": opportunity.title,
        "type": opportunity.type.value if opportunity.type else None,
        "description": opportunity.description,
        "eligibility_criteria": opportunity.eligibility_criteria,
        "funding_details": opportunity.funding_details,
    }

    try:
        result = await probability_engine_gateway.calculate_acceptance_probability(
            student_profile, opportunity_details
        )
    except Exception:
        logger.exception(
            "Acceptance probability calculation failed for user %s, opportunity %s",
            current_user.id,
            opportunity.id,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Acceptance probability calculation is temporarily unavailable.",
        )

    return CalculateProbabilityResponse(
        opportunity_id=opportunity.id,
        opportunity_title=opportunity.title,
        result=result,
    )
