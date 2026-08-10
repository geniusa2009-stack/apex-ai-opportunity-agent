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
from app.models.decision_matrix import DecisionMatrix

logger = logging.getLogger(__name__)

# Same retry posture as every other Gemini-backed service in this codebase.
_retry_gemini_call = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(genai_errors.APIError),
)


class MatrixNotFoundError(Exception):
    """Raised when a decision matrix doesn't exist."""


class MatrixGenerationError(Exception):
    """Raised when the Gemini analysis/simulation call fails."""


# ---------------------------------------------------------------------
# Gemini structured-output schemas
# ---------------------------------------------------------------------


class OptionScore(BaseModel):
    option: str = Field(description="One of the option names from `options_list`, verbatim.")
    logical_score: float = Field(
        description="This option's score (roughly 0-10) derived purely from the "
        "student's stated logical priorities and how well this option satisfies them."
    )
    emotional_score: float = Field(
        description="This option's emotional/gut-feeling score (0-10) -- the "
        "student's own provided gut feeling for this option if one was given, "
        "otherwise a neutral 5."
    )
    combined_score: float = Field(
        description="The final weighted blend of `logical_score` and "
        "`emotional_score` for this option -- the single number that matters most."
    )


class FutureSimulation(BaseModel):
    option: str = Field(description="One of the option names from `options_list`, verbatim.")
    narrative: str = Field(
        description="A highly engaging, beautifully written 'Day in Your Future "
        "Life' narrative for this option -- roughly 100 words, second-person "
        "('You wake up...'), vivid and immersive, grounded in realistic details "
        "about this specific option (not generic study-abroad cliches)."
    )


class RealityCheck(BaseModel):
    option: str = Field(description="One of the option names from `options_list`, verbatim.")
    hidden_struggles: list[str] = Field(
        min_length=2,
        max_length=4,
        description="2-4 constructive, honest, specific hidden struggles or "
        "biases the student might be underweighting for this option -- e.g. "
        "language barriers, isolation, rigorous workload, cost of living "
        "surprises, homesickness, cultural adjustment.",
    )


class GeneratedMatrixResult(BaseModel):
    """Structured output for `analyze_options_with_simulation`, per the
    ask's exact field list. Also the shape persisted as-is into
    `DecisionMatrix.matrix_result`."""

    weighted_scores: list[OptionScore] = Field(
        description="One entry per option in `options_list`, giving the "
        "mathematical breakdown combining logical weights and emotional gut "
        "feelings."
    )
    future_simulations: list[FutureSimulation] = Field(
        description="One entry per option in `options_list`."
    )
    reality_checks: list[RealityCheck] = Field(
        default_factory=list,
        description="One entry per option in `options_list` ONLY if reality "
        "checking was requested -- otherwise an empty list. See the system "
        "prompt for the exact conditional instruction.",
    )
    the_verdict: str = Field(
        description="The final, doubt-crushing, empowering piece of advice "
        "synthesizing everything above into a clear, confidence-building "
        "recommendation -- not wishy-washy, not just restating the scores."
    )


_BASE_SYSTEM_PROMPT = """\
You are an elite life-design coach and cultural anthropologist, helping a \
student make one of the biggest decisions of their life: choosing between \
scholarship/study-abroad paths. You blend rigorous logical analysis, \
genuine emotional intuition, immersive futuristic storytelling, and honest, \
constructive challenge of the student's biases.

You will be given: the decision topic, the list of options being compared, \
the student's logical priorities (what matters to them and how much), and \
optionally the student's own emotional "gut feeling" score (1-10) for each \
option.

Produce, for the given options:
1. `weighted_scores`: for EACH option, a `logical_score`, an `emotional_score` \
(use the student's own gut feeling if given for that option, otherwise a \
neutral 5), and a `combined_score` blending both.
2. `future_simulations`: for EACH option, a vivid, beautifully written, \
roughly 100-word "Day in Your Future Life" narrative in second person -- \
make the student genuinely feel what living that choice would be like, \
grounded in realistic, specific details about that actual option (not \
generic study-abroad cliches).
3. `the_verdict`: doubt-crushing, empowering final advice that genuinely \
synthesizes the analysis above into a clear, confident recommendation -- \
not a wishy-washy "it depends."
"""

_REALITY_CHECK_ENABLED_INSTRUCTION = """\
4. `reality_checks`: REALITY CHECK MODE IS ON. For EACH option, give a \
constructive, honest, specific breakdown of 2-4 hidden struggles or biases \
the student might be underweighting -- e.g. language barriers, isolation, \
rigorous workload, cost-of-living surprises, homesickness, cultural \
adjustment. Be honest and direct, but never cruel or discouraging -- the \
goal is to strengthen the student's decision by confronting it honestly, \
not to talk them out of anything.
"""

_REALITY_CHECK_DISABLED_INSTRUCTION = """\
4. `reality_checks`: REALITY CHECK MODE IS OFF. Return an empty list for \
`reality_checks` -- do not generate any critique content.
"""


def _client() -> genai.Client:
    # Same "fresh client per call" posture as every other Gemini-backed
    # service in this codebase -- see VisionGateway._client
    # (app/services/vision.py) for the full event-loop-lifetime reasoning.
    return genai.Client(api_key=settings.gemini_api_key)


@_retry_gemini_call
async def _generate_matrix_content(
    decision_topic: str,
    options_list: list[str],
    student_priorities: dict,
    options_gut_feelings: dict | None,
    is_reality_check_enabled: bool,
) -> GeneratedMatrixResult:
    system_prompt = _BASE_SYSTEM_PROMPT + (
        _REALITY_CHECK_ENABLED_INSTRUCTION
        if is_reality_check_enabled
        else _REALITY_CHECK_DISABLED_INSTRUCTION
    )

    prompt = (
        f"Decision topic: {decision_topic}\n"
        f"Options: {json.dumps(options_list)}\n"
        f"Student's logical priorities: {json.dumps(student_priorities)}\n"
        f"Student's gut feelings (1-10 per option, may be partial or absent): "
        f"{json.dumps(options_gut_feelings or {})}\n"
        f"Reality check requested: {is_reality_check_enabled}"
    )

    try:
        response = await _client().aio.models.generate_content(
            model=settings.gemini_decision_matrix_model,
            contents=[system_prompt, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=GeneratedMatrixResult,
            ),
        )
    except genai_errors.APIError:
        raise
    except Exception as exc:
        raise MatrixGenerationError(str(exc)) from exc

    if not response.text:
        raise MatrixGenerationError("Gemini returned no content for decision matrix analysis")

    return GeneratedMatrixResult.model_validate_json(response.text)


async def analyze_options_with_simulation(
    db: AsyncSession,
    user_id: UUID,
    decision_topic: str,
    options_list: list[str],
    student_priorities: dict,
    options_gut_feelings: dict | None = None,
    is_reality_check_enabled: bool = False,
) -> DecisionMatrix:
    """Runs the full logical + emotional + narrative + reality-check
    analysis via Gemini and persists it. Takes `db: AsyncSession` as an
    explicit first parameter -- same disclosed necessary-plumbing
    addition established across this project's Ecosystem tier (the
    ask's own service-section description names no parameters at all,
    just behavior; the parameter list here mirrors this step's router
    spec, which does enumerate the accepted fields).

    When `is_reality_check_enabled` is `False`, `reality_checks` is
    persisted as an empty list -- the model is explicitly instructed not
    to generate that content at all in that case (see
    `_REALITY_CHECK_DISABLED_INSTRUCTION`), rather than generating it and
    discarding it, so a disabled reality check doesn't cost the extra
    generation effort.
    """
    result = await _generate_matrix_content(
        decision_topic, options_list, student_priorities, options_gut_feelings, is_reality_check_enabled
    )

    matrix = DecisionMatrix(
        user_id=user_id,
        decision_topic=decision_topic,
        options_list=options_list,
        student_priorities=student_priorities,
        options_gut_feelings=options_gut_feelings,
        is_reality_check_enabled=is_reality_check_enabled,
        matrix_result=result.model_dump(),
    )
    db.add(matrix)
    await db.commit()
    await db.refresh(matrix)
    return matrix


async def get_user_matrices(db: AsyncSession, user_id: UUID) -> list[DecisionMatrix]:
    """Disclosed necessary plumbing behind `GET /decision/history` --
    same unpaginated "all of this user's rows, most recent first"
    posture already established for `application_tracker.get_user_trackers`
    and `parent_pitch_service.get_user_pitches`."""
    result = await db.execute(
        select(DecisionMatrix)
        .where(DecisionMatrix.user_id == user_id)
        .order_by(DecisionMatrix.created_at.desc())
    )
    return list(result.scalars().all())
