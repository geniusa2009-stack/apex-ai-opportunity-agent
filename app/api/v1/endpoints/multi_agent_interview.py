import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, DbSession, require_tokens
from app.models.opportunity import Opportunity
from app.services import coach_service
from app.services.multi_agent_interview import AgentName, conduct_agent_round

logger = logging.getLogger(__name__)

# Two path segments in the ask ("POST /multi-agent/chat") -- `prefix`
# carries the first segment, `@router.post("/chat")` the second, same
# approach as recommendations.py's `prefix="/recommendations"` +
# `@router.get("/{opportunity_id}")` for a multi-segment literal path.
router = APIRouter(prefix="/multi-agent", tags=["multi-agent-interview"])

# Ecosystem Feature 4's "Interview costs 25 tokens" example. Gated here
# specifically (rather than app/api/v1/endpoints/interview.py or
# teen_interview.py, this codebase's other two interview-flavored
# features) as a disclosed judgment call: the ask says "Interview
# Simulator" (generic, no qualifier), and this is the one of this
# codebase's THREE interview-adjacent features whose own name literally
# contains "Simulator" alongside "Interview" ("AI Multi-Agent Group
# Interview Simulator", Legendary Feature 1) -- teen_interview.py's "AI
# Friendly Teen Interview Simulator" is the other literal name match, but
# this one was chosen instead since it's also the more genuinely
# "resource-heavy" of the two (multiple simulated interviewer personas
# means multiple LLM calls per round, matching the ask's own "premium
# resource-heavy features" framing more directly than a single-call
# feature would). Not wired into interview.py (the original, un-
# "Simulator"-named Mock Interviewer) or teen_interview.py -- both remain
# free; the same `require_tokens(...)` dependency this file uses is
# trivially attachable to either of them later if that's the intended
# scope. See the README's "A genuine naming ambiguity" note for this step.
TOKEN_COST = 25


class AgentChatMessage(BaseModel):
    role: str = Field(
        description='Either "user", or one of the three agent names '
        '("Dr. Stern", "Sarah", "Alex").'
    )
    content: str = Field(min_length=1, max_length=4000)


class MultiAgentChatRequest(BaseModel):
    opportunity_id: UUID
    user_message: str = Field(min_length=1, max_length=2000)
    chat_history: list[AgentChatMessage] = Field(
        default_factory=list,
        description="Prior turns in this panel thread, oldest first. Not "
        "persisted server-side -- same posture as /chat -- resend the "
        "full history each call to maintain context.",
    )


class AgentReplyOut(BaseModel):
    agent: AgentName
    message: str


class MultiAgentChatResponse(BaseModel):
    replies: list[AgentReplyOut]


@router.post(
    "/chat",
    response_model=MultiAgentChatResponse,
    dependencies=[Depends(require_tokens("multi_agent_interview", TOKEN_COST))],
)
async def multi_agent_chat(
    payload: MultiAgentChatRequest, current_user: CurrentUser, db: DbSession
) -> MultiAgentChatResponse:
    opportunity = await db.get(Opportunity, payload.opportunity_id)
    if opportunity is None:
        # Tokens were already deducted by the `require_tokens` dependency
        # above -- refunded here since a 404 means no actual interview
        # round ran. (Failures further below, inside the `try`, are
        # refunded too -- see that block.)
        await coach_service.refund_tokens(
            db, current_user.id, TOKEN_COST, "multi_agent_interview"
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")

    opportunity_details = {
        "title": opportunity.title,
        "description": opportunity.description,
        "eligibility_criteria": opportunity.eligibility_criteria,
    }
    chat_history = [m.model_dump() for m in payload.chat_history]

    try:
        result = await conduct_agent_round(chat_history, payload.user_message, opportunity_details)
    except Exception:
        await coach_service.refund_tokens(
            db, current_user.id, TOKEN_COST, "multi_agent_interview"
        )
        logger.exception(
            "Multi-agent interview round failed for user %s, opportunity %s",
            current_user.id,
            opportunity.id,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The multi-agent interview panel is temporarily unavailable.",
        )

    return MultiAgentChatResponse(
        replies=[AgentReplyOut(agent=r.agent, message=r.message) for r in result.replies]
    )
