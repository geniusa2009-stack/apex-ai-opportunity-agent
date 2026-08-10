"""GET /wallet/dashboard and GET /wallet/opportunities -- the read-only
"Gamification & Economy Hub" surface. Composes the two token/streak
records that already exist (StudentAccountability, GamificationProfile)
rather than a new UserWallet table; see app/schemas/wallet.py's module
docstring for the full reasoning."""

from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.api.v1.endpoints.essay_humanizer import TOKEN_COST as ESSAY_HUMANIZER_COST
from app.api.v1.endpoints.multi_agent_interview import TOKEN_COST as INTERVIEW_SIM_COST
from app.schemas.wallet import OpportunityOut, WalletDashboardOut
from app.services import coach_service, gamification_service

router = APIRouter(prefix="/wallet", tags=["wallet"])


@router.get("/dashboard", response_model=WalletDashboardOut)
async def get_wallet_dashboard(current_user: CurrentUser, db: DbSession) -> WalletDashboardOut:
    account = await coach_service.get_or_create_accountability(db, current_user.id)
    profile = await gamification_service.get_or_create_profile(db, current_user.id)

    current_tier, next_tier, tokens_to_next = coach_service.compute_tier(account.total_earned_tokens)
    insight = coach_service.build_coach_insight(tokens_to_next, next_tier, profile.current_streak)

    return WalletDashboardOut(
        user_id=current_user.id,
        tokens_balance=account.tokens_balance,
        lifetime_earned=account.total_earned_tokens,
        current_tier=current_tier,
        tokens_to_next_tier=tokens_to_next,
        next_tier=next_tier,
        current_streak_days=profile.current_streak,
        longest_streak=profile.highest_streak,
        coach_insight=insight,
    )


@router.get("/opportunities", response_model=list[OpportunityOut])
async def get_wallet_opportunities() -> list[OpportunityOut]:
    """Real, currently-wired token sinks only -- no mentor-session price
    exists in the backend, so none is listed (see OpportunityOut's
    docstring)."""
    return [
        OpportunityOut(
            action="Humanize an essay draft (AI Essay Humanizer)",
            cost=ESSAY_HUMANIZER_COST,
            cost_label=f"{ESSAY_HUMANIZER_COST} Tokens",
            feature_key="essay_humanizer",
        ),
        OpportunityOut(
            action="Run a Multi-Agent Mock Interview simulation",
            cost=INTERVIEW_SIM_COST,
            cost_label=f"{INTERVIEW_SIM_COST} Tokens",
            feature_key="multi_agent_interview",
        ),
        OpportunityOut(
            action="Unlock a premium Academy course",
            cost=None,
            cost_label="Varies per course",
            feature_key="course_unlock",
        ),
    ]
