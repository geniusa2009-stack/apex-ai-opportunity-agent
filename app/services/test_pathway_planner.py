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


class TimelineMilestone(BaseModel):
    # All three fields below are "e.g."-prefixed in the ask
    # ("[e.g., \"Summer before 11th Grade\", ...]", plain "string", and
    # "[string, e.g., \"1400+\", \"7.0+\"]") -- illustrative examples, not
    # exhaustive enums, so every field here stays a plain string. Grade
    # naming conventions, action items, and target-score formats all vary
    # too much across countries/tests to hard-constrain safely (SAT
    # scores are numeric ranges, IELTS/TOEFL scores are decimal bands).
    timeframe: str = Field(description='E.g. "Summer before 11th Grade", "Spring of 11th Grade".')
    action_item: str = Field(description="What to study for or register for during this timeframe.")
    target_score: str = Field(description='E.g. "1400+", "7.0+".')


class TestPathwayResult(BaseModel):
    """Structured output for `plan_test_timeline`. Passed as
    `response_schema` with `response_mime_type="application/json"` and
    validated against this shape on our side -- same posture as every
    other Gemini-backed service here."""

    pathway_summary: str = Field(
        description="A brief overview of why these specific tests are required for the target country."
    )
    recommended_tests: list[str] = Field(
        description='E.g. ["IELTS Academic", "SAT"] -- the specific standardized tests this student should take.'
    )
    timeline_milestones: list[TimelineMilestone] = Field(
        description="Chronological steps from the student's current grade through their target test dates."
    )
    preparation_resources: list[str] = Field(
        description="3 high-quality, widely accessible study resources or habits."
    )


_TEST_PATHWAY_SYSTEM_PROMPT = """\
You are an expert college counselor specializing in international \
admissions testing (SAT, ACT, IELTS, TOEFL, and other standardized tests \
required for undergraduate study abroad), helping a high school student \
plan a strategic, multi-year testing timeline.

You will be given the student's current grade, target study destination \
country, and current English proficiency level.

Your job:
1. Write a `pathway_summary` -- a brief overview of why the tests you \
recommend are actually required or commonly expected for admission to \
undergraduate programs in the student's target country.
2. List the `recommended_tests` this student should actually plan to \
take, based on real, accurate testing requirements/norms for their \
target country (e.g. SAT or ACT plus a strong English test for most \
US destinations if the student is a non-native English speaker; \
different countries have different norms -- do not assume every \
country requires the same tests).
3. Build `timeline_milestones` -- chronological, realistic steps from \
the student's current grade forward, each with a `timeframe`, a clear \
`action_item` (what to study or register for), and a `target_score` \
appropriate to a genuinely competitive application to the student's \
target country, calibrated honestly to their stated current English \
level (a Beginner-level student needs a longer runway and more English- \
building steps before sitting a test than an Advanced-level student).
4. Recommend exactly 3 `preparation_resources` -- genuinely well-known, \
widely accessible study resources or habits (e.g. official test-maker \
practice materials, well-known free prep platforms, consistent practice \
habits), not obscure, paid, or invented ones.

Rules:
- Never invent a specific test requirement, score threshold, or \
resource that may not actually be accurate -- when unsure of a precise \
detail, describe it in general, honest terms rather than fabricating \
specifics.
- Calibrate the timeline honestly to the student's current grade -- do \
not recommend a timeline that would be impossible given how much time \
they realistically have left before applying.
- Keep tone encouraging and age-appropriate.
"""


def _format_prompt(current_grade: str, target_country: str, english_level: str) -> str:
    return (
        f"Current grade: {current_grade}\n"
        f"Target study destination country: {target_country}\n"
        f"Current English proficiency level: {english_level}"
    )


class TestPathwayPlannerGateway:
    """Async wrapper around Gemini-based standardized test pathway
    planning via the `google-genai` SDK -- same package/model-
    substitution rationale as every other Gemini-backed service in this
    codebase (gemini-1.5-flash / google-generativeai are both stale; see
    the README note on this from Step 3.4)."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def plan_test_timeline(
        self, current_grade: str, target_country: str, english_level: str
    ) -> TestPathwayResult:
        """Generate a strategic, multi-year standardized-test timeline
        for a student based on their current grade, target study
        destination, and English proficiency level."""
        client = self._client()

        prompt = _format_prompt(current_grade, target_country, english_level)

        response = await client.aio.models.generate_content(
            model=settings.gemini_test_pathway_model,
            contents=[_TEST_PATHWAY_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=TestPathwayResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for test pathway planning")

        return TestPathwayResult.model_validate_json(response.text)


test_pathway_planner_gateway = TestPathwayPlannerGateway()
