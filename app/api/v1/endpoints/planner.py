import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.api.deps import CurrentUser, DbSession
from app.models.opportunity import Opportunity
from app.services.planner import (
    RoadmapMissingDeadlineError,
    RoadmapPastDeadlineError,
    RoadmapPlan,
    planner_gateway,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/generate-roadmap", tags=["planner"])


class GenerateRoadmapRequest(BaseModel):
    opportunity_id: UUID


class GenerateRoadmapResponse(BaseModel):
    opportunity_id: UUID
    opportunity_title: str
    deadline: datetime
    roadmap: RoadmapPlan


# Path is exactly "/generate-roadmap" -- same `@router.post("")` +
# `prefix="/generate-roadmap"` pattern as chat.py/grader.py, to match the
# literal ask ("POST /generate-roadmap").
@router.post("", response_model=GenerateRoadmapResponse)
async def generate_roadmap(
    payload: GenerateRoadmapRequest, current_user: CurrentUser, db: DbSession
) -> GenerateRoadmapResponse:
    opportunity = await db.get(Opportunity, payload.opportunity_id)
    if opportunity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")

    opportunity_details = {
        "title": opportunity.title,
        "description": opportunity.description,
        "eligibility_criteria": opportunity.eligibility_criteria,
        "funding_details": opportunity.funding_details,
        "deadline": opportunity.deadline.isoformat() if opportunity.deadline else None,
    }
    current_date = datetime.now(timezone.utc).isoformat()

    try:
        roadmap = await planner_gateway.generate_roadmap(opportunity_details, current_date)
    except (RoadmapMissingDeadlineError, RoadmapPastDeadlineError) as exc:
        # These are data/precondition problems, not AI-gateway failures --
        # a 400 tells the caller what's actually wrong instead of a
        # generic "service unavailable".
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception:
        logger.exception(
            "Roadmap generation failed for user %s, opportunity %s", current_user.id, opportunity.id
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Roadmap generation is temporarily unavailable.",
        )

    return GenerateRoadmapResponse(
        opportunity_id=opportunity.id,
        opportunity_title=opportunity.title,
        deadline=opportunity.deadline,
        roadmap=roadmap,
    )
