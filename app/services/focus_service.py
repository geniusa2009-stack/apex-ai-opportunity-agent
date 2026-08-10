import logging
from datetime import datetime, timezone
from uuid import UUID

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.models.enums import FocusSessionStatus
from app.models.focus_room import FocusSession
from app.services import coach_service

logger = logging.getLogger(__name__)

# Same retry posture as every other Gemini-backed service in this codebase.
_retry_gemini_call = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(genai_errors.APIError),
)

# "Standard session tokens (15 tokens)" + "a Bonus of 10 tokens" -- the
# ask's own literal numbers, named as module constants (matching
# `coach_service.DAILY_LOGIN_TOKENS`'s precedent) rather than inlined.
STANDARD_SESSION_TOKENS = 15
QUIZ_PASS_BONUS_TOKENS = 10


class FocusSessionNotFoundError(Exception):
    """Raised when a session doesn't exist, or exists but isn't owned by
    the requesting user -- routers translate this to a 404 either way
    (the established `_get_owned_<resource>_or_404` posture: never leak
    which case it was)."""


class InvalidSessionStateError(Exception):
    """Raised when an operation is attempted against a session that isn't
    in the lifecycle state it needs to be in -- e.g. submitting a quiz
    answer before a quiz question was ever generated, or ending/abandoning
    a session that's already completed or abandoned. Not named in the
    ask, but necessary plumbing: without a state guard, calling these
    endpoints out of order would corrupt a session's fields or silently
    double-award tokens. Routers translate this to a 409 Conflict."""


class FocusSuggestionError(Exception):
    """Raised when the Gemini soundscape/brain-break suggestion call
    (part of `start_session`) fails."""


class QuizGenerationError(Exception):
    """Raised when the Gemini active-recall quiz generation call fails."""


class QuizEvaluationError(Exception):
    """Raised when the Gemini quiz-answer evaluation call fails."""


# ---------------------------------------------------------------------
# Gemini structured-output schemas
# ---------------------------------------------------------------------


class _StartSessionSuggestions(BaseModel):
    suggested_soundscape: str = Field(
        description="A specific, evocative audio soundscape STYLE NAME "
        "tailored to the student's declared goal and subject -- e.g. "
        "'Lo-Fi Focus Beats', 'Rain in Kyoto - Relaxation', 'Binaural "
        "Beats - Deep Focus', 'Classical Piano - Concentration'. A short "
        "punchy label, not a sentence."
    )
    brain_break_activity: str = Field(
        description="A short, fun, genuinely tempting 'AI Brain Break' "
        "activity for the student's break immediately after this focus "
        "block -- a quick riddle, a 30-second stretch, a 1-minute "
        "mindfulness prompt, a tiny creative challenge -- specifically "
        "designed to be more appealing in the moment than opening a "
        "social media app. Tailored to their declared goal/subject where "
        "it's natural to do so (e.g. a riddle related to their subject)."
    )


class _QuizQuestionResult(BaseModel):
    quiz_question: str = Field(
        description="A single, fun, concept-checking pop-quiz question "
        "based STRICTLY on the given study summary -- answerable using "
        "ONLY information actually present in that summary, never "
        "assuming outside knowledge the student didn't write down. "
        "Should feel like a quick, playful check-in, not an exam."
    )


class _QuizEvaluationResult(BaseModel):
    quiz_passed: bool = Field(
        description="Whether the student's answer demonstrates genuine, "
        "reasonable understanding of the concept the quiz question "
        "tested. Judge generously -- the goal is confirming real "
        "engagement with the material, not penalizing imperfect phrasing "
        "or minor omissions."
    )
    proctor_feedback: str = Field(
        description="An incredibly enthusiastic, warm, customized AI "
        "Proctor message. If passed: genuinely celebrate their focus AND "
        "their understanding, referencing something specific from their "
        "answer. If not passed: stay supportive and encouraging, gently "
        "clarify the concept, and never make them feel bad -- frame it as "
        "a normal part of learning, not a failure."
    )


class QuizSubmissionResult(BaseModel):
    """Return shape of `verify_quiz_answer` -- the ask describes this as
    a `dict`; a typed Pydantic model is used instead (informal `dict` in
    an ask has meant "structured JSON-ish result," not "literally
    untyped," everywhere else in this codebase, e.g. `PepTalkResult`)."""

    quiz_passed: bool
    proctor_feedback: str
    tokens_earned: int
    tokens_balance: int


# ---------------------------------------------------------------------
# Gemini prompts
# ---------------------------------------------------------------------

_START_SESSION_SYSTEM_PROMPT = """\
You are a fun, gamified academic personal trainer running an AI-powered \
Pomodoro focus app for students. A student is about to start a focused study \
block. Your job is to make their upcoming break irresistible in a HEALTHY \
way, so they reach for it instead of sliding into social media.

You will be given the student's declared goal for this session and the \
subject they're studying.

Your job:
1. `suggested_soundscape`: pick ONE specific, evocative background audio \
soundscape style that suits their subject/goal and would help them focus \
(e.g. deep analytical subjects might suit something calm and steady like \
binaural beats or rain; creative subjects might suit something more \
atmospheric like lo-fi). Give a short, specific, marketable-sounding label.
2. `brain_break_activity`: invent ONE short, genuinely fun activity for \
their break right after this block ends -- a riddle, a stretch, a \
micro-mindfulness prompt, a tiny creative challenge. It should be something \
a student would actually WANT to do in the 60 seconds before checking their \
phone -- playful, novel, rewarding. Where it fits naturally, tie it back to \
their subject (e.g. a science riddle for a chemistry session).

Tone: energetic, warm, a little playful -- like a personal trainer who \
genuinely wants this student to crush their goal AND enjoy the process.
"""

_QUIZ_GENERATION_SYSTEM_PROMPT = """\
You are a fun, gamified academic personal trainer running an "Active Recall" \
pop quiz feature for students who just finished a focused study block.

You will be given the student's own brief summary of what they just \
studied. Your ONLY job is to invent ONE single, fun, concept-checking \
question that tests whether they genuinely understood what they just wrote \
-- based STRICTLY on the content of that summary. Do not assume or require \
any outside knowledge beyond what's in the summary; do not ask about \
anything the summary doesn't actually mention.

Keep it light and quick -- this should feel like a fun pop quiz from an \
enthusiastic coach, not a formal exam question.
"""

_QUIZ_EVALUATION_SYSTEM_PROMPT = """\
You are an incredibly enthusiastic, supportive AI Proctor for a gamified \
student focus app, evaluating a student's answer to an Active Recall pop \
quiz.

You will be given: the student's own study summary, the pop-quiz question \
generated from it, and the student's answer to that question.

Your job:
1. Decide `quiz_passed`: does the answer show genuine, reasonable \
understanding of the concept? Judge generously and in good faith -- this is \
a quick comprehension check, not a strict exam grade. Minor phrasing issues \
or omissions shouldn't fail a student who clearly grasped the idea.
2. Write `proctor_feedback`: an incredibly enthusiastic, warm, and \
CUSTOMIZED message (reference something specific from their actual answer, \
never generic boilerplate) praising their focus. If they passed, \
celebrate their understanding too. If they didn't quite pass, stay \
supportive and encouraging, gently explain what they missed, and frame it \
as a normal, totally fine part of learning -- never make them feel bad \
about it.
"""


def _client() -> genai.Client:
    # Same "fresh client per call" posture as every other Gemini-backed
    # service in this codebase -- see VisionGateway._client
    # (app/services/vision.py) for the full event-loop-lifetime reasoning.
    return genai.Client(api_key=settings.gemini_api_key)


@_retry_gemini_call
async def _suggest_soundscape_and_brain_break(
    declared_goal: str, subject: str
) -> _StartSessionSuggestions:
    prompt = f"Declared goal: {declared_goal}\nSubject: {subject}"
    try:
        response = await _client().aio.models.generate_content(
            model=settings.gemini_focus_model,
            contents=[_START_SESSION_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_StartSessionSuggestions,
            ),
        )
    except genai_errors.APIError:
        raise
    except Exception as exc:
        raise FocusSuggestionError(str(exc)) from exc

    if not response.text:
        raise FocusSuggestionError("Gemini returned no content for session suggestions")

    return _StartSessionSuggestions.model_validate_json(response.text)


async def start_session(
    db: AsyncSession,
    user_id: UUID,
    declared_goal: str,
    subject: str,
    duration_minutes: int = 25,
) -> tuple[FocusSession, str]:
    """Creates a new in-progress `FocusSession` and returns it alongside
    the ephemeral (non-persisted) `brain_break_activity` suggestion --
    see `app/models/focus_room.py`'s docstring for why that value isn't a
    column.

    Takes `db: AsyncSession` as an explicit first parameter -- same
    disclosed necessary-plumbing addition as every other DB-writing
    service function across this project's Ecosystem tier (this ask's
    literal signature for `start_session` names no parameters at all,
    just a description of behavior).
    """
    suggestions = await _suggest_soundscape_and_brain_break(declared_goal, subject)

    session = FocusSession(
        user_id=user_id,
        declared_goal=declared_goal,
        subject=subject,
        duration_minutes=duration_minutes,
        status=FocusSessionStatus.IN_PROGRESS.value,
        suggested_soundscape=suggestions.suggested_soundscape,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)

    return session, suggestions.brain_break_activity


async def _get_owned_session(db: AsyncSession, user_id: UUID, session_id: UUID) -> FocusSession:
    """Same `_get_owned_<resource>` posture as every other user-scoped
    lookup in this codebase -- filters on `id` AND `user_id` in one WHERE
    clause so a session belonging to someone else 404s exactly like one
    that doesn't exist at all."""
    result = await db.execute(
        select(FocusSession).where(FocusSession.id == session_id, FocusSession.user_id == user_id)
    )
    session = result.scalar_one_or_none()
    if session is None:
        raise FocusSessionNotFoundError(str(session_id))
    return session


@_retry_gemini_call
async def _generate_quiz_question(study_summary: str) -> _QuizQuestionResult:
    prompt = f"Study summary:\n{study_summary}"
    try:
        response = await _client().aio.models.generate_content(
            model=settings.gemini_focus_model,
            contents=[_QUIZ_GENERATION_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_QuizQuestionResult,
            ),
        )
    except genai_errors.APIError:
        raise
    except Exception as exc:
        raise QuizGenerationError(str(exc)) from exc

    if not response.text:
        raise QuizGenerationError("Gemini returned no content for quiz generation")

    return _QuizQuestionResult.model_validate_json(response.text)


async def generate_active_recall_quiz(
    db: AsyncSession, user_id: UUID, session_id: UUID, study_summary: str
) -> _QuizQuestionResult:
    """Saves `study_summary` on the session, generates a single pop-quiz
    question strictly from it via Gemini, saves and returns
    `quiz_question`. Same disclosed `db: AsyncSession` addition as
    `start_session` above.

    Guards that the session is still `in_progress` -- calling this on an
    already-completed or abandoned session would silently overwrite a
    finished session's record with a new, dangling quiz question that
    nothing will ever evaluate.
    """
    session = await _get_owned_session(db, user_id, session_id)

    if session.status != FocusSessionStatus.IN_PROGRESS.value:
        raise InvalidSessionStateError(
            f"Session {session_id} is not in progress (status={session.status!r})"
        )

    quiz = await _generate_quiz_question(study_summary)

    session.study_summary = study_summary
    session.quiz_question = quiz.quiz_question
    db.add(session)
    await db.commit()

    return quiz


@_retry_gemini_call
async def _evaluate_quiz_answer(
    study_summary: str, quiz_question: str, student_answer: str
) -> _QuizEvaluationResult:
    prompt = (
        f"Study summary:\n{study_summary}\n\n"
        f"Quiz question asked:\n{quiz_question}\n\n"
        f"Student's answer:\n{student_answer}"
    )
    try:
        response = await _client().aio.models.generate_content(
            model=settings.gemini_focus_model,
            contents=[_QUIZ_EVALUATION_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_QuizEvaluationResult,
            ),
        )
    except genai_errors.APIError:
        raise
    except Exception as exc:
        raise QuizEvaluationError(str(exc)) from exc

    if not response.text:
        raise QuizEvaluationError("Gemini returned no content for quiz evaluation")

    return _QuizEvaluationResult.model_validate_json(response.text)


async def verify_quiz_answer(
    db: AsyncSession, user_id: UUID, session_id: UUID, student_answer: str
) -> QuizSubmissionResult:
    """Evaluates the student's answer via Gemini, awards tokens, and
    marks the session completed. Same disclosed `db: AsyncSession`
    addition as the other two functions above.

    Token award, disclosed interpretation: the ask says "If they pass...
    they get their standard session tokens (15 tokens) + a Bonus of 10
    tokens!" without stating what a FAILED quiz earns. The router-level
    description elsewhere in the same ask ("calculates/awards tokens
    (with bonus if passed)") is the tie-breaker: tokens are awarded
    either way, with the +10 bonus conditional on passing. Read this way,
    the "standard session tokens" reward the act of completing a focused
    study block itself (showing up, finishing the block, submitting a
    genuine answer) -- the quiz's pass/fail only gates the bonus, not the
    whole reward. This also fits the "incredibly enthusiastic, supportive"
    AI Proctor framing much better than a design where a single missed
    quiz question zeroes out a session's earnings entirely.

    Transactional safety, disclosed: the ask asks for "transactional
    safety." The `FocusSession` row (student_answer/quiz_passed/status/
    ended_at/tokens_earned) is updated and committed FIRST, and
    `coach_service.award_tokens` (which does its own internal commit) is
    called SECOND. True single-transaction atomicity across both tables
    isn't achieved this way -- if the token award step fails after the
    session commit succeeds, the session correctly shows as completed
    with a recorded `tokens_earned` value, but the student's actual
    token balance wouldn't yet reflect it (a real but recoverable/
    auditable inconsistency, since the session row itself is the record
    of what should have been paid out). This ordering was chosen over the
    reverse (award tokens first, then update the session) because
    awarding tokens for a session that then fails to persist as
    "completed" would be worse -- an ungrounded grant with no completed
    session to justify it. Achieving true cross-table atomicity would
    require refactoring `coach_service.award_tokens` to accept a
    shared-session/no-auto-commit mode, which is a deeper change than
    this step's scope -- noted in the README as a "Not included" item.
    """
    session = await _get_owned_session(db, user_id, session_id)

    if session.status != FocusSessionStatus.IN_PROGRESS.value:
        raise InvalidSessionStateError(
            f"Session {session_id} is not in progress (status={session.status!r})"
        )
    if not session.quiz_question or not session.study_summary:
        raise InvalidSessionStateError(
            f"Session {session_id} has no quiz question yet -- call "
            "POST /focus/end-block/{session_id} first."
        )

    evaluation = await _evaluate_quiz_answer(
        session.study_summary, session.quiz_question, student_answer
    )

    tokens_earned = STANDARD_SESSION_TOKENS + (
        QUIZ_PASS_BONUS_TOKENS if evaluation.quiz_passed else 0
    )

    session.student_answer = student_answer
    session.quiz_passed = evaluation.quiz_passed
    session.status = FocusSessionStatus.COMPLETED.value
    session.ended_at = datetime.now(timezone.utc)
    session.tokens_earned = tokens_earned
    db.add(session)
    await db.commit()

    # Reuses the token economy built in Ecosystem Feature 4 rather than
    # reinventing token-award logic here -- a deliberate, disclosed
    # integration: this ask's own vocabulary ("tokens," "Bonus") matches
    # that system exactly, and `award_tokens` already handles the
    # `StudentAccountability` row's balance/lifetime-earned bookkeeping.
    account = await coach_service.award_tokens(
        db, user_id, tokens_earned, reason=f"focus_session_completed:{session_id}"
    )

    return QuizSubmissionResult(
        quiz_passed=evaluation.quiz_passed,
        proctor_feedback=evaluation.proctor_feedback,
        tokens_earned=tokens_earned,
        tokens_balance=account.tokens_balance,
    )


async def abandon_session(db: AsyncSession, user_id: UUID, session_id: UUID) -> FocusSession:
    """Marks a session abandoned -- no tokens awarded (the session was
    never completed). Necessary plumbing behind `POST
    /focus/abandon/{session_id}`, which the ask lists as a route with no
    further description of its logic."""
    session = await _get_owned_session(db, user_id, session_id)

    if session.status != FocusSessionStatus.IN_PROGRESS.value:
        raise InvalidSessionStateError(
            f"Session {session_id} cannot be abandoned (status={session.status!r})"
        )

    session.status = FocusSessionStatus.ABANDONED.value
    session.ended_at = datetime.now(timezone.utc)
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session
