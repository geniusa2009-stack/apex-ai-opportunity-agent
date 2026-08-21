import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.deps import CurrentUser, DbSession
from app.models.career_compass import CareerCompassResult
from app.services.career_compass_service import (
    NUM_QUESTIONS,
    InvalidAnswersError,
    questions_public,
    score_riasec,
)

logger = logging.getLogger(__name__)

# Real, deterministic RIASEC (Holland Code) interest inventory. See
# app/services/career_compass_service.py for the full disclosed scoring
# methodology and app/models/career_compass.py for the persistence
# reasoning. No AI gateway call anywhere in this router.
router = APIRouter(prefix="/career-compass", tags=["career-compass"])


class CareerCompassQuestionOption(BaseModel):
    text_ar: str
    text_en: str


class CareerCompassQuestionOut(BaseModel):
    id: int
    text_ar: str
    text_en: str
    options: list[CareerCompassQuestionOption]


class CareerCompassQuestionsResponse(BaseModel):
    questions: list[CareerCompassQuestionOut]


class SubmitCareerCompassRequest(BaseModel):
    answers: list[int] = Field(
        min_length=NUM_QUESTIONS,
        max_length=NUM_QUESTIONS,
        description=f"Exactly {NUM_QUESTIONS} answers, each the 0-5 option index picked for that question.",
    )


class CareerCompassResultOut(BaseModel):
    dimension_scores: dict[str, int]
    holland_code: str
    match_percentage: float
    matched_careers: list[dict[str, str]]


@router.get("/questions", response_model=CareerCompassQuestionsResponse)
async def get_career_compass_questions(current_user: CurrentUser) -> CareerCompassQuestionsResponse:
    return CareerCompassQuestionsResponse(questions=questions_public())


@router.post("/submit", response_model=CareerCompassResultOut)
async def submit_career_compass(
    payload: SubmitCareerCompassRequest, current_user: CurrentUser, db: DbSession
) -> CareerCompassResultOut:
    try:
        result = score_riasec(payload.answers)
    except InvalidAnswersError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))

    try:
        stmt = pg_insert(CareerCompassResult).values(
            user_id=current_user.id,
            answers=payload.answers,
            dimension_scores=result.dimension_scores,
            holland_code=result.holland_code,
            match_percentage=result.match_percentage,
            matched_careers=result.matched_careers,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[CareerCompassResult.user_id],
            set_={
                "answers": stmt.excluded.answers,
                "dimension_scores": stmt.excluded.dimension_scores,
                "holland_code": stmt.excluded.holland_code,
                "match_percentage": stmt.excluded.match_percentage,
                "matched_careers": stmt.excluded.matched_careers,
            },
        )
        await db.execute(stmt)
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("Career Compass submission failed to persist for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Saving your Career Compass result is temporarily unavailable.",
        )

    return CareerCompassResultOut(
        dimension_scores=result.dimension_scores,
        holland_code=result.holland_code,
        match_percentage=result.match_percentage,
        matched_careers=result.matched_careers,
    )


@router.get("/me", response_model=CareerCompassResultOut | None)
async def get_my_career_compass_result(current_user: CurrentUser, db: DbSession) -> CareerCompassResultOut | None:
    row = await db.get(CareerCompassResult, current_user.id)
    if row is None:
        return None
    return CareerCompassResultOut(
        dimension_scores=row.dimension_scores,
        holland_code=row.holland_code,
        match_percentage=row.match_percentage,
        matched_careers=row.matched_careers,
    )
