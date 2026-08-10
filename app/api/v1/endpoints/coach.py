import logging

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, DbSession
from app.models.gamification import StudentAccountability
from app.schemas.coach import DailyClaimOut, DashboardOut
from app.services import coach_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/coach", tags=["coach"])


@router.get("/dashboard", response_model=DashboardOut)
async def get_dashboard(current_user: CurrentUser, db: DbSession) -> StudentAccountability:
    return await coach_service.get_or_create_accountability(db, current_user.id)


@router.get("/pep-talk", response_model=coach_service.PepTalkResult)
async def get_pep_talk(current_user: CurrentUser, db: DbSession) -> coach_service.PepTalkResult:
    try:
        return await coach_service.generate_pep_talk(db, current_user.id)
    except coach_service.PepTalkGenerationError as exc:
        logger.exception("Pep talk generation failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to generate pep talk: {exc}",
        ) from exc


@router.post("/daily-claim", response_model=DailyClaimOut)
async def claim_daily_tokens(current_user: CurrentUser, db: DbSession) -> DailyClaimOut:
    try:
        account = await coach_service.claim_daily_login(db, current_user.id)
    except coach_service.AlreadyClaimedTodayError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return DailyClaimOut(
        tokens_awarded=coach_service.DAILY_LOGIN_TOKENS,
        tokens_balance=account.tokens_balance,
        current_streak=account.current_streak,
    )
