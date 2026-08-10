import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.api.deps import CurrentUser, DbSession
from app.models.opportunity import Opportunity
from app.models.user import User
from app.services.quest_planner import QuestPlanResult, quest_planner_gateway

logger = logging.getLogger(__name__)

# Two path segments in the ask ("POST /quests/generate") -- `prefix`
# carries the first segment, `@router.post("/generate")` the second,
# same multi-segment approach as `/recommendation/generate`.
router = APIRouter(prefix="/quests", tags=["quest-planner"])


class GenerateQuestLineRequest(BaseModel):
    opportunity_id: UUID


class GenerateQuestLineResponse(BaseModel):
    opportunity_id: UUID
    opportunity_title: str
    result: QuestPlanResult


def _build_student_profile(user: User) -> dict:
    return {
        "major": user.major,
        "degree_level": user.degree_level,
        "gpa": user.gpa,
        "graduation_year": user.graduation_year,
        "skills": ", ".join(user.skills) if user.skills else None,
        "target_country": user.target_country,
    }


# Path is exactly "/quests/generate" -- `prefix="/quests"` +
# `@router.post("/generate")`.
@router.post("/generate", response_model=GenerateQuestLineResponse)
async def generate_quest_line(
    payload: GenerateQuestLineRequest, current_user: CurrentUser, db: DbSession
) -> GenerateQuestLineResponse:
    opportunity = await db.get(Opportunity, payload.opportunity_id)
    if opportunity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")

    # No empty-profile guard here (unlike autofill.py/probability.py/
    # pdf_filler.py) -- the opportunity's own requirements are the
    # primary source material for the quest line; the student profile is
    # personalization (academic_level_title, tailoring), not a hard
    # precondition. Same posture as planner.py's Roadmap Planner, which
    # never required a profile at all since it also only needs
    # opportunity_details.
    student_profile = _build_student_profile(current_user)
    opportunity_details = {
        "title": opportunity.title,
        "description": opportunity.description,
        "eligibility_criteria": opportunity.eligibility_criteria,
        "funding_details": opportunity.funding_details,
    }

    try:
        result = await quest_planner_gateway.generate_quest_line(student_profile, opportunity_details)
    except Exception:
        logger.exception(
            "Quest line generation failed for user %s, opportunity %s",
            current_user.id,
            opportunity.id,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Quest line generation is temporarily unavailable.",
        )

    return GenerateQuestLineResponse(
        opportunity_id=opportunity.id,
        opportunity_title=opportunity.title,
        result=result,
    )
