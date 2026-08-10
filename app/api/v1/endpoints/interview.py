import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, DbSession
from app.models.opportunity import Opportunity
from app.services.interview import evaluate_answer, generate_interview_questions

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/interview", tags=["interview"])


class InterviewStartRequest(BaseModel):
    opportunity_id: UUID


class InterviewStartResponse(BaseModel):
    opportunity_id: UUID
    opportunity_title: str
    questions: list[str]


class InterviewEvaluateRequest(BaseModel):
    opportunity_id: UUID
    question: str = Field(min_length=1, max_length=1000)
    user_answer: str = Field(min_length=1, max_length=5000)


class InterviewEvaluateResponse(BaseModel):
    opportunity_id: UUID
    question: str
    score: int
    feedback: str
    ideal_answer: str


async def _get_opportunity_or_404(opportunity_id: UUID, db: AsyncSession) -> Opportunity:
    opportunity = await db.get(Opportunity, opportunity_id)
    if opportunity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")
    return opportunity


def _opportunity_details(opportunity: Opportunity) -> dict:
    return {
        "title": opportunity.title,
        "description": opportunity.description,
        "eligibility_criteria": opportunity.eligibility_criteria,
        "funding_details": opportunity.funding_details,
    }


@router.post("/start", response_model=InterviewStartResponse)
async def start_interview(
    payload: InterviewStartRequest, current_user: CurrentUser, db: DbSession
) -> InterviewStartResponse:
    opportunity = await _get_opportunity_or_404(payload.opportunity_id, db)

    try:
        questions = await generate_interview_questions(_opportunity_details(opportunity))
    except Exception:
        logger.exception(
            "Interview question generation failed for user %s, opportunity %s",
            current_user.id,
            opportunity.id,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Interview question generation is temporarily unavailable.",
        )

    return InterviewStartResponse(
        opportunity_id=opportunity.id, opportunity_title=opportunity.title, questions=questions
    )


@router.post("/evaluate", response_model=InterviewEvaluateResponse)
async def evaluate_interview_answer(
    payload: InterviewEvaluateRequest, current_user: CurrentUser, db: DbSession
) -> InterviewEvaluateResponse:
    opportunity = await _get_opportunity_or_404(payload.opportunity_id, db)

    try:
        evaluation = await evaluate_answer(
            payload.question, payload.user_answer, _opportunity_details(opportunity)
        )
    except Exception:
        logger.exception(
            "Answer evaluation failed for user %s, opportunity %s", current_user.id, opportunity.id
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Answer evaluation is temporarily unavailable.",
        )

    return InterviewEvaluateResponse(
        opportunity_id=opportunity.id,
        question=payload.question,
        score=evaluation["score"],
        feedback=evaluation["feedback"],
        ideal_answer=evaluation["ideal_answer"],
    )
