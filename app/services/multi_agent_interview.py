import logging
from typing import Literal

from pydantic import BaseModel, Field

from app.services.groq_client import build_chat_groq, retry_groq_call

logger = logging.getLogger(__name__)

AgentName = Literal["Dr. Stern", "Sarah", "Alex"]


class AgentReply(BaseModel):
    agent: AgentName
    message: str = Field(description="This agent's in-character reply for this round.")


class AgentRoundResult(BaseModel):
    """Structured output for `conduct_agent_round`. Wrapper so
    `with_structured_output` can be used (same rationale as
    interview.py's `InterviewQuestions`) -- the ask's example shape was a
    bare JSON list (`[{"agent": ..., "message": ...}]`), but every
    structured-output call in this codebase validates against a
    top-level `BaseModel`, not a bare list, so this wraps it in a
    `replies` field. `.replies` is what callers actually consume."""

    replies: list[AgentReply] = Field(
        description="The next 1 or 2 agent replies for this round, in the "
        "order they should be shown. Not every agent needs to speak in "
        "every round."
    )


# --- The 3 distinct panel personas ---
# Defined as separate named system-prompt blocks per the ask ("Define 3
# distinct agent system prompts"), then combined into ONE orchestrator
# prompt for a single Groq call -- exactly the latency-saving approach
# the ask itself suggested ("to keep it clean and save latency, you can
# make a single orchestration call"), rather than 3 separate LLM calls
# per round.

_DR_STERN_PERSONA = """\
Dr. Stern (Academic Critic): Focuses strictly on methodology, research gaps, \
and academic rigor. Direct, precise, unimpressed by vague or unsubstantiated \
claims. Pushes the candidate to justify their reasoning and cite specifics. \
Not unkind, but not warm either -- respects candidates who can defend their \
thinking under scrutiny.\
"""

_SARAH_PERSONA = """\
Sarah (HR / Behavioral): Warm but genuinely probing. Focuses on soft skills, \
motivation, and culture fit. Asks follow-up questions that dig past a \
rehearsed answer to find out what the candidate actually cares about. \
Encouraging in tone, but not a pushover -- she notices when an answer is \
generic.\
"""

_ALEX_PERSONA = """\
Alex (Competitive Peer Candidate): Plays a rival candidate sitting in on \
the same panel, not an interviewer. Occasionally interrupts or jumps in \
with his own competitive response to the same question, to show how a \
strong rival would answer and put healthy pressure on the candidate. Alex \
should NOT speak in every round -- only jump in when it's natural for a \
competitive peer to interject (e.g. after a question with an obvious angle \
to one-up, not after every single exchange). Confident, a little \
competitive, but not hostile or rude.\
"""

_ORCHESTRATOR_SYSTEM_PROMPT = f"""\
You are running a live, multi-personality mock panel interview with three \
distinct participants reacting to a student candidate, all in one shared \
conversation thread. You will generate the next 1 or 2 replies in this \
thread, each fully in-character.

The three participants:

1. {_DR_STERN_PERSONA}

2. {_SARAH_PERSONA}

3. {_ALEX_PERSONA}

You will be given the opportunity this interview is for, the conversation \
so far (all prior user and agent messages, in order), and the candidate's \
newest message. Your job:

1. Decide which 1 or 2 of the three participants should respond next, based \
on what would naturally happen in a real live panel -- not a rigid rotation. \
Dr. Stern and Sarah are the primary interviewers and will respond most \
often; Alex only jumps in occasionally, when it's natural for a competitive \
peer to interject (see Alex's persona above) -- do not include Alex in \
every round.
2. Write each selected participant's reply fully in their own distinct \
voice and focus, as described in their persona above. The three \
participants must sound clearly different from each other -- do not let \
their tone blur together.
3. Keep each reply grounded in what the candidate has actually said (in \
this message and the conversation so far) and in the opportunity's actual \
details -- do not invent facts about the candidate's background or the \
opportunity that weren't actually given.
4. Keep replies conversational and reasonably concise -- this is a live \
back-and-forth, not a written essay.

Rules:
- Only use the `agent` values "Dr. Stern", "Sarah", or "Alex" -- exactly as \
spelled here.
- Do not have a participant repeat a point they (or another participant) \
already made earlier in the conversation -- move the conversation forward.
- Do not fabricate specific facts, credentials, papers, or statistics -- \
neither about the candidate nor the opportunity -- beyond what was \
actually provided.
"""


def _format_opportunity(opportunity_details: dict) -> str:
    lines = [f"Title: {opportunity_details.get('title', 'Unknown')}"]
    if opportunity_details.get("description"):
        lines.append(f"Description: {opportunity_details['description']}")
    if opportunity_details.get("eligibility_criteria"):
        lines.append(f"Eligibility criteria: {opportunity_details['eligibility_criteria']}")
    return "\n".join(lines)


def _format_chat_history(chat_history: list[dict]) -> str:
    if not chat_history:
        return "(No prior messages -- this is the start of the interview.)"
    lines = []
    for turn in chat_history:
        speaker = turn.get("role", "unknown")
        content = turn.get("content", "")
        lines.append(f"{speaker}: {content}")
    return "\n".join(lines)


@retry_groq_call
async def conduct_agent_round(
    chat_history: list[dict], user_message: str, opportunity_details: dict
) -> AgentRoundResult:
    """Generate the next 1-2 agent replies for the panel interview thread.

    `chat_history` is a list of `{"role": ..., "content": ...}` dicts,
    oldest first, where `role` is either `"user"` or one of the three
    agent names -- same "no server-side session storage, resend full
    history each call" posture as `chat.py`'s single-agent chatbot.

    `opportunity_details` (expects title/description/eligibility_criteria
    keys) is a parameter beyond the ask's literal 2-argument signature
    (`chat_history`, `user_message`) -- added because the router fetches
    the opportunity specifically "to anchor context," and the panel has
    nowhere to receive that context without a third parameter. Disclosed
    the same way other necessary signature additions have been throughout
    this project (e.g. `vision.py`'s `mime_type`, `recommendation.py`'s
    `exclude_opportunity_id`).
    """
    llm = build_chat_groq(temperature=0.7)
    structured_llm = llm.with_structured_output(AgentRoundResult)

    prompt = (
        f"Opportunity:\n{_format_opportunity(opportunity_details)}\n\n"
        f"Conversation so far:\n{_format_chat_history(chat_history)}\n\n"
        f"Candidate's newest message: {user_message}"
    )

    result = await structured_llm.ainvoke(
        [
            {"role": "system", "content": _ORCHESTRATOR_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
    )
    if not isinstance(result, AgentRoundResult):
        result = AgentRoundResult.model_validate(result)
    return result
