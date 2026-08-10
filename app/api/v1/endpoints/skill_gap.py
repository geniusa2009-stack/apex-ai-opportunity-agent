import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, DbSession
from app.models.opportunity import Opportunity
from app.services.skill_gap import SkillGapAnalysis, analyze_skill_gap

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/skill-gap", tags=["skill-gap"])


class SkillGapRequest(BaseModel):
    opportunity_id: UUID
    user_background: str = Field(
        min_length=20,
        max_length=5000,
        description="Free-text background: education, experience, skills, relevant history.",
    )


class SkillGapResponse(BaseModel):
    opportunity_id: UUID
    opportunity_title: str
    analysis: SkillGapAnalysis
    analyzed_at: datetime


@router.post("/analyze", response_model=SkillGapResponse)
async def analyze_skill_gap_endpoint(
    payload: SkillGapRequest, current_user: CurrentUser, db: DbSession
) -> SkillGapResponse:
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
        analysis = await analyze_skill_gap(opportunity_details, payload.user_background)
    except Exception:
        logger.exception(
            "Skill gap analysis failed for user %s, opportunity %s", current_user.id, opportunity.id
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Skill gap analysis is temporarily unavailable.",
        )

    return SkillGapResponse(
        opportunity_id=opportunity.id,
        opportunity_title=opportunity.title,
        analysis=analysis,
        analyzed_at=datetime.now(timezone.utc),
    )
