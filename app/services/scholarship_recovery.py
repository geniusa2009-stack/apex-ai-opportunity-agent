import logging

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, Field
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings

logger = logging.getLogger(__name__)

# Same retry posture as every other Gemini-backed service in this codebase:
# `errors.ClientError` (429s) and `errors.ServerError` (5xx) both inherit
# from `errors.APIError`.
_retry_gemini_call = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(genai_errors.APIError),
)


class ScholarshipRecoveryResult(BaseModel):
    """Structured output for `generate_recovery_plan`. Passed as
    `response_schema` with `response_mime_type="application/json"` and
    validated against this shape on our side -- same posture as every
    other Gemini-backed service here."""

    resilience_message: str = Field(
        description="A warm, highly encouraging opening paragraph emphasizing that rejection is "
        "redirection and part of the process."
    )
    weakness_breakdown: str = Field(
        description="A constructive analysis of the perceived weaknesses, explaining why they matter "
        "to committees without making the student feel bad."
    )
    profile_rebuilding_plan: list[str] = Field(
        description="3-4 chronological, specific, actionable steps to turn weaknesses into strengths "
        "over the next year."
    )
    alternative_targets: list[str] = Field(
        description="2 alternative or upcoming scholarships/programs to focus on next."
    )


_SCHOLARSHIP_RECOVERY_SYSTEM_PROMPT = """\
You are an incredibly supportive, empathetic resilience coach and \
strategic admissions counselor, helping a high school student process a \
rejection from an early opportunity (like a summer camp or junior \
scholarship) and turn it into a concrete growth plan. Rejection is \
genuinely tough for teenagers, and they need constructive feedback, not \
discouragement.

You will be given the scholarship/program name, the student's own \
perceived weaknesses, and their current grade.

Your job:
1. Write a `resilience_message` -- warm and genuinely encouraging, \
validating that disappointment after a rejection is a normal and valid \
feeling (do not dismiss or minimize it), while emphasizing that this \
specific rejection is redirection, not a verdict on their worth or \
potential, and is a completely normal part of the process almost every \
successful applicant goes through somewhere along the way.
2. Write a `weakness_breakdown` -- a constructive, honest analysis of \
why the student's stated perceived weaknesses genuinely matter to \
selection committees, framed in a way that explains the reasoning \
without making the student feel judged or ashamed -- the goal is \
understanding, not blame.
3. Write a `profile_rebuilding_plan` -- exactly 3-4 chronological, \
concrete, actionable steps the student can take over the next year to \
genuinely turn these specific weaknesses into strengths -- not vague \
platitudes like "try harder," but specific actions tied to their actual \
stated weaknesses and realistic given their current grade.
4. Write `alternative_targets` -- exactly 2 alternative or upcoming \
scholarships/programs genuinely worth focusing on next, appropriate to \
the student's current grade and profile -- only reference genuinely \
well-known, real programs, never an invented name.

Rules:
- Never use dismissive or minimizing language about the student's \
disappointment (e.g. avoid phrases like "it's not a big deal" or "just \
move on") -- validate the feeling first, then pivot to constructive \
next steps.
- Avoid toxic positivity -- genuine encouragement acknowledges the \
setback is real while still being hopeful, rather than pretending the \
rejection didn't matter.
- Never invent a specific reason for the rejection you don't actually \
know, and never invent a specific alternative scholarship/program name \
that may not actually exist.
- Keep tone warm, genuinely caring, and psychologically healthy \
throughout -- this is meant to help a teenager process a real setback \
constructively.
"""


def _format_prompt(
    scholarship_name: str, perceived_weaknesses: list[str], current_grade: str
) -> str:
    return (
        f"Scholarship/program name: {scholarship_name}\n"
        f"Perceived weaknesses: {', '.join(perceived_weaknesses)}\n"
        f"Current grade: {current_grade}"
    )


class ScholarshipRecoveryGateway:
    """Async wrapper around Gemini-based scholarship rejection recovery
    plan generation via the `google-genai` SDK -- same package/model-
    substitution rationale as every other Gemini-backed service in this
    codebase (gemini-1.5-flash / google-generativeai are both stale; see
    the README note on this from Step 3.4).

    Distinct from `appeal_drafter.py` (Legendary Feature 9, Step 3.33) --
    that feature drafts an actual APPEAL LETTER to send back to an
    institution asking for reconsideration; this feature instead
    generates purely internal, forward-looking resilience/planning
    content for the student themselves, with no institution-facing
    document produced. Different concern entirely, no overlap."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def generate_recovery_plan(
        self, scholarship_name: str, perceived_weaknesses: list[str], current_grade: str
    ) -> ScholarshipRecoveryResult:
        """Generate a warm, constructive recovery plan turning a
        student's perceived weaknesses from a rejection into a concrete
        profile-rebuilding strategy for the next application cycle."""
        client = self._client()

        prompt = _format_prompt(scholarship_name, perceived_weaknesses, current_grade)

        response = await client.aio.models.generate_content(
            model=settings.gemini_scholarship_recovery_model,
            contents=[_SCHOLARSHIP_RECOVERY_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ScholarshipRecoveryResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for scholarship recovery plan generation")

        return ScholarshipRecoveryResult.model_validate_json(response.text)


scholarship_recovery_gateway = ScholarshipRecoveryGateway()
