import logging
import uuid

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, DbSession
from app.schemas.focus import (
    AbandonSessionOut,
    EndBlockOut,
    EndBlockRequest,
    FocusSessionOut,
    StartSessionOut,
    StartSessionRequest,
    SubmitQuizOut,
    SubmitQuizRequest,
)
from app.services import focus_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/focus", tags=["focus"])


@router.post("/start", response_model=StartSessionOut, status_code=status.HTTP_201_CREATED)
async def start_focus_session(
    payload: StartSessionRequest, current_user: CurrentUser, db: DbSession
) -> StartSessionOut:
    try:
        session, brain_break_activity = await focus_service.start_session(
            db, current_user.id, payload.declared_goal, payload.subject, payload.duration_minutes
        )
    except focus_service.FocusSuggestionError as exc:
        logger.exception("Focus session start suggestions failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to generate session suggestions: {exc}",
        ) from exc

    # `brain_break_activity` isn't a column on `FocusSession` (see
    # app/models/focus_room.py's docstring for why) -- validate the ORM
    # object against the base schema first, then layer the ephemeral
    # value on top, rather than validating straight into `StartSessionOut`
    # (which would fail: the ORM object has no such attribute).
    base = FocusSessionOut.model_validate(session)
    return StartSessionOut(**base.model_dump(), brain_break_activity=brain_break_activity)


@router.post("/end-block/{session_id}", response_model=EndBlockOut)
async def end_focus_block(
    session_id: uuid.UUID, payload: EndBlockRequest, current_user: CurrentUser, db: DbSession
) -> EndBlockOut:
    try:
        quiz = await focus_service.generate_active_recall_quiz(
            db, current_user.id, session_id, payload.study_summary
        )
    except focus_service.FocusSessionNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Focus session not found"
        ) from exc
    except focus_service.InvalidSessionStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except focus_service.QuizGenerationError as exc:
        logger.exception("Active recall quiz generation failed for session %s", session_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to generate pop-quiz question: {exc}",
        ) from exc

    return EndBlockOut(session_id=session_id, quiz_question=quiz.quiz_question)


@router.post("/submit-quiz/{session_id}", response_model=SubmitQuizOut)
async def submit_focus_quiz(
    session_id: uuid.UUID, payload: SubmitQuizRequest, current_user: CurrentUser, db: DbSession
) -> SubmitQuizOut:
    try:
        result = await focus_service.verify_quiz_answer(
            db, current_user.id, session_id, payload.student_answer
        )
    except focus_service.FocusSessionNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Focus session not found"
        ) from exc
    except focus_service.InvalidSessionStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except focus_service.QuizEvaluationError as exc:
        logger.exception("Quiz answer evaluation failed for session %s", session_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to evaluate quiz answer: {exc}",
        ) from exc

    return SubmitQuizOut(
        session_id=session_id,
        quiz_passed=result.quiz_passed,
        proctor_feedback=result.proctor_feedback,
        tokens_earned=result.tokens_earned,
        tokens_balance=result.tokens_balance,
    )


@router.post("/abandon/{session_id}", response_model=AbandonSessionOut)
async def abandon_focus_session(
    session_id: uuid.UUID, current_user: CurrentUser, db: DbSession
) -> AbandonSessionOut:
    try:
        session = await focus_service.abandon_session(db, current_user.id, session_id)
    except focus_service.FocusSessionNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Focus session not found"
        ) from exc
    except focus_service.InvalidSessionStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return AbandonSessionOut(session_id=session.id, status=session.status)
