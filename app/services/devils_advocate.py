import logging

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, Field
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings

logger = logging.getLogger(__name__)

# Same retry posture as the other Gemini-backed services in this
# codebase: `errors.ClientError` (429s) and `errors.ServerError` (5xx)
# both inherit from `errors.APIError`.
_retry_gemini_call = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(genai_errors.APIError),
)


class CriticismResult(BaseModel):
    """Structured output for `criticize_proposal`. Passed as
    `response_schema` with `response_mime_type="application/json"` and
    validated against this shape on our side -- same posture as every
    other Gemini-backed service here.

    NOTE on `vulnerability_score` direction: every other score field in
    this codebase (grader's `score`, skill_gap's `overall_fit_score`,
    voice_coach's `content_score`) is "higher is better." This one is
    deliberately inverted -- higher means MORE logical gaps, i.e. a
    riskier proposal -- because that's literally what was asked for. A
    frontend rendering this alongside the other scores must not treat a
    high number here as good; it should probably be shown as a risk
    gauge, not a score bar, to avoid that mix-up.
    """

    vulnerability_score: int = Field(
        ge=0,
        le=100,
        description="0-100. Higher means MORE logical gaps / weaker "
        "proposal -- this is inverted relative to this project's other "
        "score fields, where higher is normally better.",
    )
    critical_questions: list[str] = Field(
        default_factory=list,
        description="3-5 tough, direct questions an admissions/review "
        "committee would actually ask about this specific proposal.",
    )
    weak_points: list[str] = Field(
        default_factory=list,
        description="Concrete logical flaws or gaps in the proposal's "
        "methodology, feasibility, or significance -- grounded in what "
        "the proposal actually says (or conspicuously fails to say).",
    )
    defense_strategies: list[str] = Field(
        default_factory=list,
        description="Actionable advice for how the student could either "
        "revise the proposal or verbally defend it against the "
        "critical_questions above.",
    )


_DEVILS_ADVOCATE_SYSTEM_PROMPT = """\
You are a rigorous, highly skeptical academic reviewer sitting on an \
admissions or grant review committee. You have read hundreds of proposals \
and are not easily impressed -- your job is to stress-test this student's \
research proposal or study plan the way a genuinely critical reviewer would, \
so the student can prepare for tough questions before they face a real \
committee.

You will be given the student's proposal or study plan as text. Your job:

1. Critically analyze the proposal's methodology -- is it sound, specific, \
and appropriate for the stated goals, or vague/hand-wavy/mismatched?
2. Critically analyze its feasibility -- given the stated timeline, \
resources, and the student's apparent background, is this actually \
achievable, or overly ambitious/underspecified?
3. Critically analyze its significance -- does it clearly articulate why \
this work matters, or does it assert importance without justifying it?
4. Challenge the student's unstated assumptions -- what is the proposal \
taking for granted that a skeptical reviewer would push back on?

Rules:
- Ground every criticism in what the proposal actually says (or \
conspicuously omits) -- do not fabricate a claim, method, or detail the \
proposal didn't actually make and then criticize that instead. It is fine \
to draw on general academic/methodological knowledge to identify a real \
weakness (e.g. a known confound in a proposed study design), but the \
weakness itself must be tied to something genuinely present or absent in \
the given text, not invented context about the student or field that \
wasn't provided.
- Be genuinely rigorous, not performatively harsh -- the goal is to find \
real, defensible weaknesses a real reviewer would raise, not to invent \
criticism for its own sake. If the proposal is genuinely strong in some \
dimension, do not manufacture a fake flaw there just to fill the list.
- `critical_questions` should read like actual questions a reviewer would \
ask out loud in an interview or committee review, not restated criticisms.
- `defense_strategies` must be concrete and actionable -- specific edits to \
make or specific points to raise verbally -- not generic advice like "be \
more confident."
"""


class DevilsAdvocateGateway:
    """Async wrapper around Gemini-based proposal criticism via the
    `google-genai` SDK -- same package/model-substitution rationale as
    every other Gemini-backed service in this codebase (gemini-1.5-flash
    / google-generativeai are both stale; see the README note on this
    from Step 3.4)."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def criticize_proposal(self, proposal_text: str) -> CriticismResult:
        """Critique a student's research proposal or study plan from a
        rigorous, skeptical academic reviewer's perspective."""
        client = self._client()

        prompt = f"Student's proposal / study plan:\n{proposal_text}"

        response = await client.aio.models.generate_content(
            model=settings.gemini_devils_advocate_model,
            contents=[_DEVILS_ADVOCATE_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=CriticismResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for proposal criticism")

        return CriticismResult.model_validate_json(response.text)


devils_advocate_gateway = DevilsAdvocateGateway()
