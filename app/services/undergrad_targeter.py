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


class FutureOpportunity(BaseModel):
    scholarship_name: str
    typical_deadline_month: str = Field(
        description="The month this scholarship typically opens/closes for applications, described "
        "in general terms if the exact month varies by year."
    )
    core_requirements: str = Field(description="The core eligibility/application requirements.")


class UndergradTargeterResult(BaseModel):
    """Structured output for `target_future_scholarships`. Passed as
    `response_schema` with `response_mime_type="application/json"` and
    validated against this shape on our side -- same posture as every
    other Gemini-backed service here."""

    strategic_outlook: str = Field(
        description="A brief, encouraging paragraph about the advantage of starting early from this grade."
    )
    future_opportunities: list[FutureOpportunity] = Field(
        description="3 major undergraduate scholarships in the student's preferred regions fitting their "
        "target majors."
    )
    profile_gaps: list[str] = Field(
        description="2-3 things high schoolers typically lack early on, like leadership roles or "
        "specific language tests."
    )
    multi_year_action_plan: list[str] = Field(
        description="A chronological list of broad steps from the current grade until graduation."
    )


_UNDERGRAD_TARGETER_SYSTEM_PROMPT = """\
You are a strategic, long-term college admissions planner, helping a \
high school freshman or sophomore understand what undergraduate \
scholarships will realistically be available to them in the future, so \
they can start building a competitive profile years in advance.

You will be given the student's current grade, target undergraduate \
majors, and preferred study regions.

Your job:
1. Write a `strategic_outlook` -- a brief, genuinely encouraging \
paragraph about the real advantage of starting this early from their \
current grade.
2. List exactly 3 `future_opportunities` -- genuinely real, well-known \
major undergraduate scholarships available in the student's preferred \
regions that plausibly fit their target majors. Only name scholarships \
you are confident genuinely exist -- never invent a scholarship name. \
For `typical_deadline_month`, describe the general timing honestly \
(e.g. "typically opens in the fall, closes around January") rather than \
stating an exact date you aren't certain is accurate, since these can \
shift year to year. Give honest `core_requirements`.
3. List 2-3 `profile_gaps` -- things high schoolers this early in their \
journey typically haven't built up yet (e.g. leadership roles, specific \
language test scores, research experience) that they should start \
working on now.
4. Write a `multi_year_action_plan` -- a chronological list of broad, \
realistic steps from the student's current grade through graduation to \
build toward these scholarships, genuinely paced across the years \
remaining rather than front-loaded or unrealistic.

Rules:
- Never invent a scholarship name, deadline, or requirement that may \
not actually be accurate -- when uncertain of a precise detail, speak \
in general, honest terms and note that details should be verified \
closer to application time since programs change.
- Keep the plan genuinely paced across the years the student actually \
has remaining, not compressed into an unrealistic timeline.
- Keep tone encouraging and strategic, appropriate for a student who is \
years away from actually applying.
"""


def _format_prompt(
    current_grade: str, target_majors: list[str], preferred_regions: list[str]
) -> str:
    return (
        f"Current grade: {current_grade}\n"
        f"Target majors: {', '.join(target_majors)}\n"
        f"Preferred regions: {', '.join(preferred_regions)}"
    )


class UndergradTargeterGateway:
    """Async wrapper around Gemini-based future undergraduate
    scholarship targeting via the `google-genai` SDK -- same
    package/model-substitution rationale as every other Gemini-backed
    service in this codebase (gemini-1.5-flash / google-generativeai are
    both stale; see the README note on this from Step 3.4)."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def target_future_scholarships(
        self, current_grade: str, target_majors: list[str], preferred_regions: list[str]
    ) -> UndergradTargeterResult:
        """Project future undergraduate scholarship opportunities and
        identify current profile gaps for an early-stage high school
        student, based on their grade, target majors, and preferred
        regions."""
        client = self._client()

        prompt = _format_prompt(current_grade, target_majors, preferred_regions)

        response = await client.aio.models.generate_content(
            model=settings.gemini_undergrad_targeter_model,
            contents=[_UNDERGRAD_TARGETER_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=UndergradTargeterResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for undergrad scholarship targeting")

        return UndergradTargeterResult.model_validate_json(response.text)


undergrad_targeter_gateway = UndergradTargeterGateway()
