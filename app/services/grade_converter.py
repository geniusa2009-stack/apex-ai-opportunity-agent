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


class GradeConversion(BaseModel):
    # All three ask fields here are "e.g."-prefixed ("[e.g., \"US 4.0
    # GPA\"]", "[e.g., \"3.8/4.0\"]", "[e.g., \"Highly Competitive\"]")
    # -- illustrative examples, not exhaustive enums, so every field
    # stays a plain string. Grading systems and competitiveness
    # framing genuinely vary far more than a short example list, and
    # this is an estimate feature anyway (see official_evaluation_
    # disclaimer below), so forcing a rigid enum would be both
    # inaccurate to the real diversity of grading systems and false
    # precision on an inherently approximate conversion.
    target_system: str = Field(description='E.g. "US 4.0 GPA".')
    estimated_equivalent: str = Field(description='E.g. "3.8/4.0".')
    competitiveness_level: str = Field(description='E.g. "Highly Competitive".')


class GradeConversionResult(BaseModel):
    """Structured output for `convert_grades`. Passed as
    `response_schema` with `response_mime_type="application/json"` and
    validated against this shape on our side -- same posture as every
    other Gemini-backed service here."""

    conversion_summary: str = Field(description="A brief explanation of the calculation logic used.")
    conversions: list[GradeConversion] = Field(
        description="One conversion entry per requested target grading system."
    )
    official_evaluation_disclaimer: str = Field(
        description="A reminder that this is an AI estimate and official university evaluations may vary."
    )


_GRADE_CONVERTER_SYSTEM_PROMPT = """\
You are an expert international admissions credential evaluator, \
similar to organizations like WES (World Education Services), helping a \
high school student understand how their local grade translates to \
international grading systems used by scholarships and universities \
abroad.

You will be given the student's local grade, their local grading \
system, and a list of target grading systems to convert to.

Your job:
1. Write a `conversion_summary` -- a brief, honest explanation of the \
calculation logic you used to estimate each conversion (e.g. percentile \
mapping, standard published conversion tables for that country pair), \
specific enough that a student could understand roughly how the number \
was derived.
2. Produce one `conversions` entry per requested target system, each \
with the `target_system` name, an `estimated_equivalent` in that \
system's own native format, and an honest `competitiveness_level` \
assessment (e.g. how competitive this grade would look to admissions \
officers evaluating that specific system).
3. Write an `official_evaluation_disclaimer` -- clearly reminding the \
student that this is an AI-generated estimate only, and that official \
university or credential evaluation services (like WES, ECE, or a \
university's own admissions office) may arrive at a different official \
figure, and that this estimate should not be treated as a substitute \
for an official evaluation.

Rules:
- Base every conversion on genuine, widely-recognized grading-scale \
knowledge for the specific systems involved -- do not invent a \
conversion methodology or number that has no reasonable basis.
- Be honest about uncertainty -- grade conversion between very different \
systems is inherently approximate, and the disclaimer exists precisely \
because this is an estimate, not an official figure.
- Keep tone clear, reassuring, and genuinely helpful for a student \
who may find this whole process confusing.
"""


def _format_prompt(local_grade: str, local_grading_system: str, target_systems: list[str]) -> str:
    return (
        f"Local grade: {local_grade}\n"
        f"Local grading system: {local_grading_system}\n"
        f"Target grading systems: {', '.join(target_systems)}"
    )


class GradeConverterGateway:
    """Async wrapper around Gemini-based international grade conversion
    via the `google-genai` SDK -- same package/model-substitution
    rationale as every other Gemini-backed service in this codebase
    (gemini-1.5-flash / google-generativeai are both stale; see the
    README note on this from Step 3.4)."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def convert_grades(
        self, local_grade: str, local_grading_system: str, target_systems: list[str]
    ) -> GradeConversionResult:
        """Estimate the conversion of a student's local grade to one or
        more target international grading systems."""
        client = self._client()

        prompt = _format_prompt(local_grade, local_grading_system, target_systems)

        response = await client.aio.models.generate_content(
            model=settings.gemini_grade_converter_model,
            contents=[_GRADE_CONVERTER_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=GradeConversionResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for grade conversion")

        return GradeConversionResult.model_validate_json(response.text)


grade_converter_gateway = GradeConverterGateway()
