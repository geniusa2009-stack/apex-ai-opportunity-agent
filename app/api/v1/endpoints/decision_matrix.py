import logging

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, DbSession
from app.models.decision_matrix import DecisionMatrix
from app.schemas.decision_matrix import AnalyzeRequest, MatrixOut
from app.services import decision_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/decision", tags=["decision-matrix"])


@router.post("/analyze", response_model=MatrixOut, status_code=status.HTTP_201_CREATED)
async def analyze_decision(
    payload: AnalyzeRequest, current_user: CurrentUser, db: DbSession
) -> DecisionMatrix:
    try:
        return await decision_service.analyze_options_with_simulation(
            db,
            current_user.id,
            payload.decision_topic,
            payload.options_list,
            payload.student_priorities,
            payload.options_gut_feelings,
            payload.is_reality_check_enabled,
        )
    except decision_service.MatrixGenerationError as exc:
        logger.exception("Decision matrix analysis failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to generate decision analysis: {exc}",
        ) from exc


@router.get("/history", response_model=list[MatrixOut])
async def decision_history(current_user: CurrentUser, db: DbSession) -> list[DecisionMatrix]:
    return await decision_service.get_user_matrices(db, current_user.id)
