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


class RecommendedProgram(BaseModel):
    program_name: str
    # Judgment call, disclosed: the ask lists "host_countries" and
    # "core_values_sought" as fields with no explicit type. Both are
    # plural-named and naturally multi-valued -- a program like YES or
    # FLEX spans many host countries, and "values sought" reads as a
    # short list of traits (e.g. adaptability, leadership, resilience)
    # rather than one long descriptive sentence. Modeled as list[str]
    # for both, matching the plural naming, rather than a single string.
    host_countries: list[str] = Field(description="Countries this program places students in.")
    core_values_sought: list[str] = Field(
        description="The traits/values this program's selection process looks for, e.g. adaptability, leadership."
    )
    description: str


class CulturalInterviewQuestion(BaseModel):
    question_text: str = Field(
        description="A behavioral interview question focused on adaptability, homesickness, or host-family conflict."
    )
    strategy_to_answer: str = Field(description="A concrete strategy for how the student should approach answering.")


class ExchangeAdvisorResult(BaseModel):
    """Structured output for `advise_exchange_programs`. Passed as
    `response_schema` with `response_mime_type="application/json"` and
    validated against this shape on our side -- same posture as every
    other Gemini-backed service here."""

    advisor_intro: str = Field(
        description="A brief, enthusiastic introduction about the value of cultural exchange."
    )
    recommended_programs: list[RecommendedProgram] = Field(
        description="2-3 specific fully funded cultural exchange programs fitting the student's target region."
    )
    cultural_interview_prep: list[CulturalInterviewQuestion] = Field(
        description="3 behavioral interview questions with a strategy to answer each."
    )
    application_timeline_advice: str = Field(
        description="A short overview of when these programs typically open and close for applications."
    )


_EXCHANGE_ADVISOR_SYSTEM_PROMPT = """\
You are an expert cultural exchange coordinator, advising a high school \
student on fully funded cultural exchange programs (like YES, FLEX, UWC, \
or ASSIST) -- programs that select for cultural adaptability and \
leadership rather than pure academic achievement.

You will be given the student's target region, current grade, and level \
of community involvement.

Your job:
1. Write an `advisor_intro` -- brief and genuinely enthusiastic about the \
value of cultural exchange.
2. Recommend 2-3 `recommended_programs` genuinely suited to the \
student's target region. Only reference real, genuinely well-known \
fully funded exchange programs (e.g. YES, FLEX, UWC, ASSIST, or similarly \
well-known ones) that plausibly operate in or place students toward the \
student's stated target region -- do not invent a program name that does \
not actually exist. For each, list its real `host_countries`, the \
`core_values_sought` in its selection process, and a clear `description`.
3. Write `cultural_interview_prep` -- exactly 3 behavioral interview \
questions genuinely focused on adaptability, homesickness, or handling \
conflict with a host family, each paired with a concrete \
`strategy_to_answer` -- not generic interview advice, but specific to \
the emotional/cultural-adjustment nature of these questions.
4. Write `application_timeline_advice` -- a short, honest overview of \
when these kinds of programs typically open and close applications \
(general seasonal patterns, since exact dates shift year to year).

Rules:
- Never invent a specific program name, host country list, or deadline \
that may not actually be accurate -- when unsure of a precise detail, \
describe it in general, honest terms rather than fabricating specifics.
- Take the student's level of community involvement into account \
honestly -- these programs value demonstrated community engagement, so \
note where a student with low involvement might want to build more \
before applying, without being discouraging.
- Keep tone warm, encouraging, and age-appropriate.
"""


def _format_prompt(target_region: str, current_grade: str, community_involvement_level: str) -> str:
    return (
        f"Target region: {target_region}\n"
        f"Current grade: {current_grade}\n"
        f"Community involvement level: {community_involvement_level}"
    )


class ExchangeAdvisorGateway:
    """Async wrapper around Gemini-based cultural exchange program
    advising via the `google-genai` SDK -- same package/model-
    substitution rationale as every other Gemini-backed service in this
    codebase (gemini-1.5-flash / google-generativeai are both stale; see
    the README note on this from Step 3.4)."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def advise_exchange_programs(
        self, target_region: str, current_grade: str, community_involvement_level: str
    ) -> ExchangeAdvisorResult:
        """Recommend fully funded cultural exchange programs and prep a
        student for the cultural interview, based on their target
        region, current grade, and community involvement level."""
        client = self._client()

        prompt = _format_prompt(target_region, current_grade, community_involvement_level)

        response = await client.aio.models.generate_content(
            model=settings.gemini_exchange_advisor_model,
            contents=[_EXCHANGE_ADVISOR_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ExchangeAdvisorResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for exchange program advising")

        return ExchangeAdvisorResult.model_validate_json(response.text)


exchange_advisor_gateway = ExchangeAdvisorGateway()
