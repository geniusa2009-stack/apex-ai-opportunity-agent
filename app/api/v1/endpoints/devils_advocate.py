import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.services.devils_advocate import CriticismResult, devils_advocate_gateway

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/criticize-proposal", tags=["devils-advocate"])


class CriticizeProposalRequest(BaseModel):
    proposal_text: str = Field(
        min_length=10,
        max_length=12000,
        description="The student's research proposal or study plan to critique.",
    )


class CriticizeProposalResponse(BaseModel):
    result: CriticismResult


# Path is exactly "/criticize-proposal" -- same `@router.post("")` +
# `prefix="/criticize-proposal"` pattern used by every other
# single-purpose POST endpoint since Feature 1.
@router.post("", response_model=CriticizeProposalResponse)
async def criticize_proposal(
    payload: CriticizeProposalRequest, current_user: CurrentUser
) -> CriticizeProposalResponse:
    # No opportunity/DB lookup here -- same precedent as polisher.py
    # (Feature 7) and professor.py (Advanced Feature 1): this operates
    # purely on the text block the caller supplies, no opportunity
    # context needed or requested.
    try:
        result = await devils_advocate_gateway.criticize_proposal(payload.proposal_text)
    except Exception:
        logger.exception("Proposal criticism failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Proposal criticism is temporarily unavailable.",
        )

    return CriticizeProposalResponse(result=result)
