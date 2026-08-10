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

# The ask specifies this feature's own age bounds (12-19) explicitly,
# unlike Step 3.36's competition scout where the 5-25 range was a
# disclosed judgment call. Enforced both here (service-level, in case
# this method is ever called from somewhere other than the router) and
# again at the request-schema level in the router, matching the layered
# validation approach nowhere else strictly required but cheap insurance.
MIN_AGE = 12
MAX_AGE = 19


class InvalidAgeError(ValueError):
    """Raised when `age` falls outside the 12-19 range this feature is
    scoped to (pre-college summer programs)."""


class TopProgram(BaseModel):
    program_name: str
    host_institution: str
    # "[e.g., Research, Leadership, Coding]" IS "e.g."-prefixed -- an
    # illustrative example list, not an exhaustive enum, so this stays a
    # plain string. Real programs span far more categories than three
    # (Arts, Business, Medicine, Debate, ...), so hard-constraining here
    # would be actively wrong for many legitimate programs.
    program_type: str = Field(description="E.g. Research, Leadership, Coding.")
    # "[e.g., \"Full scholarships available\", \"Self-funded\"]" is also
    # "e.g."-prefixed -- same reasoning, plain string. Real funding
    # situations are a spectrum (need-based aid, merit scholarships,
    # partial aid, payment plans), not a clean two-value split.
    funding_availability: str = Field(description='E.g. "Full scholarships available", "Self-funded".')
    description: str


class SummerProgramMatchResult(BaseModel):
    """Structured output for `match_summer_programs`. Passed as
    `response_schema` with `response_mime_type="application/json"` and
    validated against this shape on our side -- same posture as every
    other Gemini-backed service here."""

    advisory_summary: str = Field(
        description="A brief explanation of how summer programs boost a profile for this student's specific major."
    )
    top_programs: list[TopProgram] = Field(
        description="3-4 specific pre-college summer programs genuinely suited to this student's age, "
        "major interest, and financial need level."
    )
    application_strategy: list[str] = Field(
        description="3 steps on how to prepare a strong application for these types of programs."
    )


_SUMMER_PROGRAM_SYSTEM_PROMPT = """\
You are an elite pre-college advisor specializing in prestigious summer \
programs (research programs, leadership institutes, STEM camps, pre- \
college university courses, and similar) that meaningfully strengthen a \
high schooler's global scholarship and undergraduate admissions profile.

You will be given the student's age, academic major interest, and \
financial need level.

Your job:
1. Write an `advisory_summary` -- a brief explanation of how summer \
programs boost a competitive profile specifically for this student's \
stated major, not generic advice.
2. Recommend 3-4 `top_programs` genuinely suited to the student's age, \
major interest, and financial need level. Only reference real, genuinely \
well-known programs and institutions (e.g. well-known university-hosted \
pre-college programs, well-known research/leadership institutes) that \
plausibly match the student's stated age -- do not recommend a program \
that would realistically exclude a student of this age, and do not \
invent a program or host-institution name that does not actually exist. \
For each, give the `program_name`, `host_institution`, `program_type`, \
an honest `funding_availability` assessment consistent with the \
student's stated financial need level (do not recommend an unaffordable, \
self-funded-only program to a student who needs full funding without \
flagging that mismatch), and a clear `description`.
3. Write an `application_strategy` of exactly 3 concrete steps on how to \
prepare a strong application for these specific types of programs.

Rules:
- Never invent a specific program name, host institution, deadline, or \
funding detail that may not actually exist -- when unsure of a precise \
detail, describe it in general, honest terms rather than fabricating \
specifics.
- Take the student's financial need level seriously -- do not recommend \
programs that are a poor financial fit without saying so honestly.
- Keep tone encouraging and age-appropriate.
"""


def _format_prompt(age: int, major_interest: str, financial_need_level: str) -> str:
    return (
        f"Age: {age}\n"
        f"Major interest: {major_interest}\n"
        f"Financial need level: {financial_need_level}"
    )


class SummerProgramMatchmakerGateway:
    """Async wrapper around Gemini-based summer program matchmaking via
    the `google-genai` SDK -- same package/model-substitution rationale
    as every other Gemini-backed service in this codebase
    (gemini-1.5-flash / google-generativeai are both stale; see the
    README note on this from Step 3.4)."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def match_summer_programs(
        self, age: int, major_interest: str, financial_need_level: str
    ) -> SummerProgramMatchResult:
        """Match a student with high-impact pre-college summer programs
        based on their age, major interest, and financial need level."""
        if not (MIN_AGE <= age <= MAX_AGE):
            raise InvalidAgeError(
                f"age must be between {MIN_AGE} and {MAX_AGE} (pre-college summer programs), got {age}"
            )

        client = self._client()

        prompt = _format_prompt(age, major_interest, financial_need_level)

        response = await client.aio.models.generate_content(
            model=settings.gemini_summer_program_model,
            contents=[_SUMMER_PROGRAM_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=SummerProgramMatchResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for summer program matchmaking")

        return SummerProgramMatchResult.model_validate_json(response.text)


summer_program_matchmaker_gateway = SummerProgramMatchmakerGateway()
