import logging
import uuid

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, DbSession
from app.models.budget_planner import BudgetPlanner
from app.schemas.budget_planner import BudgetOut, SimulateRequest
from app.services import budget_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/budget", tags=["budget-planner"])


@router.post("/simulate", response_model=BudgetOut, status_code=status.HTTP_201_CREATED)
async def simulate_budget(
    payload: SimulateRequest, current_user: CurrentUser, db: DbSession
) -> BudgetPlanner:
    try:
        return await budget_service.simulate_budget(
            db, current_user.id, payload.target_city, payload.target_country, payload.monthly_stipend
        )
    except budget_service.BudgetGenerationError as exc:
        logger.exception("Budget simulation failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to simulate budget: {exc}",
        ) from exc


@router.get("/history", response_model=list[BudgetOut])
async def budget_history(current_user: CurrentUser, db: DbSession) -> list[BudgetPlanner]:
    return await budget_service.get_user_budgets(db, current_user.id)


@router.delete("/{budget_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_budget(budget_id: uuid.UUID, current_user: CurrentUser, db: DbSession) -> None:
    try:
        await budget_service.delete_budget(db, current_user.id, budget_id)
    except budget_service.BudgetNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Budget plan not found") from exc
