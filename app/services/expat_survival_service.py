import json
import logging
from uuid import UUID

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.models.expat_survival import ExpatSurvival
from app.services import coach_service

logger = logging.getLogger(__name__)

# Same retry posture as every other Gemini-backed service in this codebase.
_retry_gemini_call = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(genai_errors.APIError),
)

TOTAL_TURNS = 3

# "standard tokens (15) + a 'Street Smart' bonus of 10 tokens" -- the
# ask's own literal numbers, named as module constants, matching
# `focus_service.STANDARD_SESSION_TOKENS`/`guild_service.
# STANDARD_COMMENT_TOKENS`'s precedent.
STANDARD_RPG_TOKENS = 15
STREET_SMART_BONUS_TOKENS = 10


class SessionNotFoundError(Exception):
    """Raised when a session doesn't exist, or exists but isn't owned by
    the requesting user."""


class InvalidSessionStateError(Exception):
    """Raised when `continue_rpg_session` is called on a session that has
    already ended (`is_completed=True`)."""


class SessionGenerationError(Exception):
    """Raised when the Gemini start-of-game call (slang dictionary +
    opening scenario) fails."""


class TurnGenerationError(Exception):
    """Raised when the Gemini turn-generation call fails."""


# ---------------------------------------------------------------------
# Gemini structured-output schemas
# ---------------------------------------------------------------------


class SlangEntry(BaseModel):
    word: str = Field(description="A real local slang word or expression used in this city/country.")
    phonetic_arabic: str = Field(
        description="A phonetic pronunciation guide for this word, written using Arabic "
        "letters to approximate the sound (e.g. for the German 'Servus' -> "
        "'بِتنطق: زير-فوس'). Always start with 'بِتنطق:' followed by the phonetic rendering."
    )
    usage_context: str = Field(
        description="A short, practical explanation of when and how locals actually use this word."
    )


class StartSessionResult(BaseModel):
    slang_dictionary: list[SlangEntry] = Field(min_length=3, max_length=3)
    opening_scenario: str = Field(
        description="The first turn of a highly engaging, localized, witty text-RPG "
        "scenario grounded in a real, specific situation in this city (e.g. getting lost "
        "in the Berlin U-Bahn) -- end with a clear situation the student must respond to."
    )


class ContinueTurnResult(BaseModel):
    next_turn_narrative: str = Field(
        description="The next turn of the story, reacting genuinely to the student's "
        "input and continuing the scenario -- witty, immersive, and specific to the city/"
        "country. If this is the final turn, also narrate a satisfying conclusion to the "
        "scenario within this text."
    )
    landmine_triggered_this_turn: bool = Field(
        description="Whether the student's input this turn hit a genuine cultural taboo "
        "or social 'landmine' for this country."
    )
    landmine_explanation: str | None = Field(
        default=None,
        description="If a landmine was triggered: a humorous but genuinely educational "
        "explanation of the cultural mistake, woven into how the story reacts. Null if no landmine.",
    )
    final_survival_score: int | None = Field(
        default=None,
        ge=0,
        le=100,
        description="ONLY set if this is the final turn (the game is ending): a 0-100 "
        "survival score reflecting how well the student navigated the scenario overall.",
    )
    used_slang_correctly: bool | None = Field(
        default=None,
        description="ONLY meaningful if this is the final turn: whether the student used "
        "any of the given slang dictionary words appropriately at some point in the conversation.",
    )


class ContinueSessionResult(BaseModel):
    """Return shape of `continue_rpg_session` -- the ask describes this
    as a `dict`; a typed Pydantic model is used instead, same "informal
    dict means structured Pydantic model" posture already established in
    `focus_service.QuizSubmissionResult`/`guild_service.
    CommentSubmissionResult`."""

    session_id: UUID
    current_step: int
    next_turn_narrative: str
    landmine_triggered_this_turn: bool
    landmine_explanation: str | None
    is_completed: bool
    final_survival_score: int | None
    tokens_earned: int | None
    tokens_balance: int | None


# ---------------------------------------------------------------------
# Gemini prompts
# ---------------------------------------------------------------------

_START_SYSTEM_PROMPT = """\
You are a witty, culturally immersive game master running a text-based "AI \
Expat RPG" that teaches international students real local slang and \
cultural know-how through a fun, branching 3-turn scenario.

You will be given a target city and country. Produce:
1. `slang_dictionary`: EXACTLY 3 real local slang words/expressions, each \
with a phonetic pronunciation guide written using ARABIC LETTERS to \
approximate the sound (always starting with 'بِتنطق:'), and a short usage \
context.
2. `opening_scenario`: the first turn of an engaging, witty, highly \
localized scenario grounded in a real, specific everyday situation in this \
city (e.g. getting lost in a specific real transit system, ordering at a \
specific kind of local shop, navigating a real cultural custom). End with a \
clear situation the student needs to respond to.
"""

_CONTINUE_SYSTEM_PROMPT_TEMPLATE = """\
You are the same witty, culturally immersive game master continuing this \
"AI Expat RPG" scenario for {city}, {country}. This is turn {current_turn} \
of {total_turns}.

You will be given: the slang dictionary offered at the start, the full \
conversation so far, and the student's latest response.

Your job:
1. Check whether the student's latest response hit a genuine cultural \
taboo/social "landmine" for {country} (e.g. a real social faux pas, not a \
trivial phrasing issue). If so, set `landmine_triggered_this_turn` to true \
and weave a humorous but genuinely educational explanation into how the \
story reacts -- never mean-spirited, always framed as "now you know!"
2. Continue the story with `next_turn_narrative`, reacting genuinely to \
what the student did, staying witty and immersive.
{final_turn_instruction}
"""

_FINAL_TURN_INSTRUCTION = """\
3. THIS IS THE FINAL TURN -- bring the scenario to a satisfying conclusion \
within `next_turn_narrative`. Also set `final_survival_score` (0-100, \
reflecting overall performance across the whole conversation) and \
`used_slang_correctly` (whether the student used any of the given slang \
words appropriately at any point).
"""

_NON_FINAL_TURN_INSTRUCTION = """\
3. This is NOT the final turn -- leave `final_survival_score` and \
`used_slang_correctly` unset (null); the scenario continues after this.
"""


def _client() -> genai.Client:
    # Same "fresh client per call" posture as every other Gemini-backed
    # service in this codebase -- see VisionGateway._client
    # (app/services/vision.py) for the full event-loop-lifetime reasoning.
    return genai.Client(api_key=settings.gemini_api_key)


@_retry_gemini_call
async def _generate_start_content(city: str, country: str) -> StartSessionResult:
    prompt = f"Target city: {city}\nTarget country: {country}"
    try:
        response = await _client().aio.models.generate_content(
            model=settings.gemini_expat_survival_model,
            contents=[_START_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=StartSessionResult,
            ),
        )
    except genai_errors.APIError:
        raise
    except Exception as exc:
        raise SessionGenerationError(str(exc)) from exc

    if not response.text:
        raise SessionGenerationError("Gemini returned no content for RPG session start")

    return StartSessionResult.model_validate_json(response.text)


async def start_rpg_session(db: AsyncSession, user_id: UUID, city: str, country: str) -> ExpatSurvival:
    """Generates a slang dictionary + opening scenario via Gemini and
    creates a new session. Takes `db: AsyncSession` as an explicit first
    parameter -- same disclosed necessary-plumbing addition established
    across this project's Ecosystem tier (this ask's literal signature
    omits it)."""
    result = await _generate_start_content(city, country)

    session = ExpatSurvival(
        user_id=user_id,
        target_city=city,
        target_country=country,
        current_step=1,
        chat_history=[{"role": "ai", "text": result.opening_scenario}],
        slang_dictionary=[entry.model_dump() for entry in result.slang_dictionary],
        cultural_landmine_triggered=False,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def _get_owned_session(db: AsyncSession, user_id: UUID, session_id: UUID) -> ExpatSurvival:
    """Same `_get_owned_<resource>` posture as every other user-scoped
    lookup in this codebase."""
    result = await db.execute(
        select(ExpatSurvival).where(ExpatSurvival.id == session_id, ExpatSurvival.user_id == user_id)
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise SessionNotFoundError(str(session_id))
    return session


@_retry_gemini_call
async def _generate_turn_content(
    city: str,
    country: str,
    current_turn: int,
    slang_dictionary: list[dict],
    chat_history: list[dict],
    is_final_turn: bool,
) -> ContinueTurnResult:
    system_prompt = _CONTINUE_SYSTEM_PROMPT_TEMPLATE.format(
        city=city,
        country=country,
        current_turn=current_turn,
        total_turns=TOTAL_TURNS,
        final_turn_instruction=_FINAL_TURN_INSTRUCTION if is_final_turn else _NON_FINAL_TURN_INSTRUCTION,
    )
    prompt = (
        f"Slang dictionary offered at the start:\n{json.dumps(slang_dictionary)}\n\n"
        f"Conversation so far:\n{json.dumps(chat_history)}"
    )

    try:
        response = await _client().aio.models.generate_content(
            model=settings.gemini_expat_survival_model,
            contents=[system_prompt, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ContinueTurnResult,
            ),
        )
    except genai_errors.APIError:
        raise
    except Exception as exc:
        raise TurnGenerationError(str(exc)) from exc

    if not response.text:
        raise TurnGenerationError("Gemini returned no content for RPG turn generation")

    return ContinueTurnResult.model_validate_json(response.text)


async def continue_rpg_session(
    db: AsyncSession, user_id: UUID, session_id: UUID, student_input: str
) -> ContinueSessionResult:
    """Appends the student's message, generates the next turn (detecting
    cultural landmines), and -- once `current_step` reaches
    `TOTAL_TURNS` -- ends the game, scores it, and awards tokens.

    Token award, disclosed interpretation: the ask says the final turn
    "awards standard tokens (15) + a 'Street Smart' bonus of 10 tokens if
    they avoided all landmines and used slang correctly" -- silent on
    what a session WITHOUT that achievement earns. Same posture as every
    other token-award judgment call in this project's Ecosystem tier
    (Feature 5's `verify_quiz_answer`, Feature 6's
    `submit_comment_with_rewrite`): the standard 15 tokens reward
    completing the game itself, and the +10 bonus is conditional on BOTH
    stated achievements (`cultural_landmine_triggered` staying `False`
    across the WHOLE session -- a sticky, session-wide flag, not just
    this turn -- AND Gemini's own `used_slang_correctly` verdict on the
    final turn).

    Takes `db: AsyncSession` as an explicit first parameter, same
    disclosed necessary-plumbing addition as every other DB-writing
    function in this project.
    """
    session = await _get_owned_session(db, user_id, session_id)

    if session.is_completed:
        raise InvalidSessionStateError(f"Session {session_id} has already ended")

    # Reassign the whole list (rather than `.append`) so SQLAlchemy's
    # JSONB change-tracking actually picks up the mutation -- same
    # pattern already established for `ApplicationTracker.ai_todo_list`.
    updated_history = session.chat_history + [{"role": "user", "text": student_input}]

    next_step = session.current_step + 1
    is_final_turn = next_step >= TOTAL_TURNS

    turn = await _generate_turn_content(
        session.target_city,
        session.target_country,
        next_step,
        session.slang_dictionary,
        updated_history,
        is_final_turn,
    )

    updated_history = updated_history + [{"role": "ai", "text": turn.next_turn_narrative}]

    session.chat_history = updated_history
    session.current_step = next_step
    if turn.landmine_triggered_this_turn:
        session.cultural_landmine_triggered = True

    tokens_earned: int | None = None
    tokens_balance: int | None = None

    if is_final_turn:
        session.is_completed = True
        session.final_survival_score = turn.final_survival_score

        earned_bonus = (not session.cultural_landmine_triggered) and bool(turn.used_slang_correctly)
        tokens_earned = STANDARD_RPG_TOKENS + (STREET_SMART_BONUS_TOKENS if earned_bonus else 0)
        session.tokens_earned = tokens_earned

    db.add(session)
    await db.commit()

    if is_final_turn:
        # Reuses the token economy built in Ecosystem Feature 4, same
        # deliberate integration already established in Features 5 and 6.
        account = await coach_service.award_tokens(
            db, user_id, tokens_earned, reason=f"expat_survival_completed:{session_id}"
        )
        tokens_balance = account.tokens_balance

    return ContinueSessionResult(
        session_id=session.id,
        current_step=session.current_step,
        next_turn_narrative=turn.next_turn_narrative,
        landmine_triggered_this_turn=turn.landmine_triggered_this_turn,
        landmine_explanation=turn.landmine_explanation,
        is_completed=session.is_completed,
        final_survival_score=session.final_survival_score,
        tokens_earned=tokens_earned,
        tokens_balance=tokens_balance,
    )
