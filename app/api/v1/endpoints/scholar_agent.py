import logging
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models.agent_alert import AgentAlert
from app.services.web_explorer import LiveOpportunityResult
from app.workers.scholar_agent import run_autonomous_scholar_search

logger = logging.getLogger(__name__)

# Two path segments in the ask ("POST /scholar-agent/trigger", "GET
# /scholar-agent/results") -- `prefix` carries the first segment, each
# route decorator the second, same multi-segment approach as
# multi_agent_interview.py's "/multi-agent/chat".
router = APIRouter(prefix="/scholar-agent", tags=["scholar-agent"])


class TriggerScholarAgentResponse(BaseModel):
    task_id: str


class AgentAlertOut(BaseModel):
    id: UUID
    search_query: str
    report_text: str
    matches: list[LiveOpportunityResult]
    created_at: datetime


@router.post("/trigger", response_model=TriggerScholarAgentResponse)
async def trigger_scholar_agent(current_user: CurrentUser) -> TriggerScholarAgentResponse:
    # Precondition check before dispatching -- same posture as
    # autofill.py/probability.py's all-empty-profile guards. Without
    # this, a caller with no profile data set would dispatch a Celery
    # task that's guaranteed to no-op (the task independently re-checks
    # this too -- see scholar_agent.py's docstring on why), only to poll
    # for a task_id that never produces a useful result. Failing fast
    # here is more actionable than a silent no-op discovered later.
    profile_is_empty = not any(
        [current_user.major, current_user.degree_level, current_user.gpa, current_user.target_country]
    )
    if profile_is_empty:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No profile information is set yet. Update your profile "
            "via PATCH /api/v1/users/me first.",
        )

    async_result = run_autonomous_scholar_search.delay(str(current_user.id))
    return TriggerScholarAgentResponse(task_id=async_result.id)


@router.get("/results", response_model=list[AgentAlertOut])
async def get_scholar_agent_results(
    current_user: CurrentUser,
    db: DbSession,
    limit: int = Query(default=10, ge=1, le=50),
) -> list[AgentAlertOut]:
    result = await db.execute(
        select(AgentAlert)
        .where(AgentAlert.user_id == current_user.id)
        .order_by(AgentAlert.created_at.desc())
        .limit(limit)
    )
    alerts = result.scalars().all()
    return [
        AgentAlertOut(
            id=a.id,
            search_query=a.search_query,
            report_text=a.report_text,
            matches=a.matches,
            created_at=a.created_at,
        )
        for a in alerts
    ]
