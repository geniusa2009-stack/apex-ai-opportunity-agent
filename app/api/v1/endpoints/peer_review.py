import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.services.peer_review import PanelReviewResult, peer_review_gateway

logger = logging.getLogger(__name__)

# Two path segments in the ask ("POST /peer-review/simulate") --
# `prefix` carries the first segment, `@router.post("/simulate")` the
# second, same multi-segment approach as multi_agent_interview.py's
# "/multi-agent/chat" and scholar_agent.py's "/scholar-agent/trigger".
router = APIRouter(prefix="/peer-review", tags=["peer-review"])


class SimulatePeerReviewRequest(BaseModel):
    proposal_text: str = Field(
        min_length=10,
        max_length=12000,
        description="The student's research proposal to simulate a peer-review panel for.",
    )


class SimulatePeerReviewResponse(BaseModel):
    result: PanelReviewResult


@router.post("/simulate", response_model=SimulatePeerReviewResponse)
async def simulate_peer_review(
    payload: SimulatePeerReviewRequest, current_user: CurrentUser
) -> SimulatePeerReviewResponse:
    # No opportunity/DB lookup here -- same precedent as polisher.py
    # (Feature 7), devils_advocate.py (Advanced Feature 7), and
    # cultural_localizer.py (Legendary Feature 2): this operates purely
    # on the text supplied, no opportunity/DB context needed.
    try:
        result = await peer_review_gateway.simulate_panel_review(payload.proposal_text)
    except Exception:
        logger.exception("Peer-review panel simulation failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Peer-review panel simulation is temporarily unavailable.",
        )

    return SimulatePeerReviewResponse(result=result)
