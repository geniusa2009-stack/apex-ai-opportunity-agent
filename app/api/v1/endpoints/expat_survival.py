import logging
import uuid

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, DbSession
from app.schemas.expat_survival import PlayRequest, PlayTurnOut, StartRequest, StartSessionOut
from app.services import expat_survival_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/expat-survival", tags=["expat-survival"])


@router.post("/start", response_model=StartSessionOut, status_code=status.HTTP_201_CREATED)
async def start_session(
    payload: StartRequest, current_user: CurrentUser, db: DbSession
) -> StartSessionOut:
    try:
        session = await expat_survival_service.start_rpg_session(
            db, current_user.id, payload.target_city, payload.target_country
        )
    except expat_survival_service.SessionGenerationError as exc:
        logger.exception("Expat RPG session start failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to start Expat RPG session: {exc}",
        ) from exc

    return StartSessionOut(
        session_id=session.id,
        target_city=session.target_city,
        target_country=session.target_country,
        current_step=session.current_step,
        slang_dictionary=session.slang_dictionary,
        opening_scenario=session.chat_history[0]["text"],
    )


@router.post("/play/{session_id}", response_model=PlayTurnOut)
async def play_turn(
    session_id: uuid.UUID, payload: PlayRequest, current_user: CurrentUser, db: DbSession
) -> PlayTurnOut:
    try:
        result = await expat_survival_service.continue_rpg_session(
            db, current_user.id, session_id, payload.student_input
        )
    except expat_survival_service.SessionNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Expat RPG session not found"
        ) from exc
    except expat_survival_service.InvalidSessionStateError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except expat_survival_service.TurnGenerationError as exc:
        logger.exception("Expat RPG turn generation failed for session %s", session_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to generate next turn: {exc}",
        ) from exc

    return PlayTurnOut(
        session_id=result.session_id,
        current_step=result.current_step,
        next_turn_narrative=result.next_turn_narrative,
        landmine_triggered_this_turn=result.landmine_triggered_this_turn,
        landmine_explanation=result.landmine_explanation,
        is_completed=result.is_completed,
        final_survival_score=result.final_survival_score,
        tokens_earned=result.tokens_earned,
        tokens_balance=result.tokens_balance,
    )
