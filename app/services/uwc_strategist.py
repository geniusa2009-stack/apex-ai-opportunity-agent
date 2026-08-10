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


class UwcStrategyResult(BaseModel):
    """Structured output for `generate_uwc_strategy`. Passed as
    `response_schema` with `response_mime_type="application/json"` and
    validated against this shape on our side -- same posture as every
    other Gemini-backed service here."""

    value_alignment_summary: str = Field(
        description="How the student's specific passion aligns with UWC's mission of peace and a sustainable future."
    )
    essay_angles: list[str] = Field(
        description="3 specific, compelling essay angles/stories drawn from the student's inputs."
    )
    leadership_action_plan: list[str] = Field(
        description="3 actionable steps the student can take locally to demonstrate initiative before applying."
    )
    selection_day_tips: list[str] = Field(
        description="3 tips for the UWC group interview/selection day, focused on collaboration over competition."
    )


_UWC_STRATEGIST_SYSTEM_PROMPT = """\
You are an expert United World Colleges (UWC) National Committee \
selector, helping a high school student strategize their UWC scholarship \
application. UWC's admissions process heavily emphasizes world peace, \
sustainability, intercultural understanding, and personal initiative --  \
UWC deliberately selects for values and character over pure academic \
achievement, and its selection days are famously built around \
collaborative group activities rather than competitive ones.

You will be given the student's passion/cause and their current \
activities.

Your job:
1. Write a `value_alignment_summary` -- a genuine, specific explanation \
of how this student's particular passion connects to UWC's mission of \
peace and a sustainable future, grounded in their actual stated passion \
rather than generic UWC mission language.
2. Identify 3 `essay_angles` -- specific, compelling story angles this \
student could actually write about, drawn directly from their stated \
passion and current activities, not generic essay advice that could \
apply to any student.
3. Build a `leadership_action_plan` -- exactly 3 concrete, realistic \
steps the student can take locally (in their own community/school) to \
demonstrate initiative before they apply, genuinely connected to their \
stated passion and activities.
4. Write `selection_day_tips` -- exactly 3 tips specific to UWC's group \
interview/selection day format, emphasizing genuine collaboration, \
active listening, and supporting other candidates rather than trying to \
outshine them -- UWC selectors specifically watch for candidates who \
lift up the group, and candidates who behave competitively are viewed \
unfavorably.

Rules:
- Ground every recommendation specifically in what the student actually \
told you -- avoid generic advice that doesn't reference their real \
passion or activities.
- Do not invent a specific UWC campus, program detail, or selection \
procedure fact that may not actually be accurate -- speak in general, \
honest terms about UWC's known values and selection philosophy.
- Keep tone encouraging and age-appropriate.
"""


def _format_prompt(passion_cause: str, current_activities: list[str]) -> str:
    return (
        f"Passion/cause: {passion_cause}\n"
        f"Current activities: {', '.join(current_activities)}"
    )


class UwcStrategistGateway:
    """Async wrapper around Gemini-based UWC scholarship strategy
    generation via the `google-genai` SDK -- same package/model-
    substitution rationale as every other Gemini-backed service in this
    codebase (gemini-1.5-flash / google-generativeai are both stale; see
    the README note on this from Step 3.4)."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def generate_uwc_strategy(
        self, passion_cause: str, current_activities: list[str]
    ) -> UwcStrategyResult:
        """Map a student's passion and current activities to UWC's core
        values, producing an essay/interview strategy for their UWC
        scholarship application."""
        client = self._client()

        prompt = _format_prompt(passion_cause, current_activities)

        response = await client.aio.models.generate_content(
            model=settings.gemini_uwc_strategist_model,
            contents=[_UWC_STRATEGIST_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=UwcStrategyResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for UWC strategy generation")

        return UwcStrategyResult.model_validate_json(response.text)


uwc_strategist_gateway = UwcStrategistGateway()
