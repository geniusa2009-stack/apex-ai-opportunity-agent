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


class CultureShockScenario(BaseModel):
    scenario_description: str = Field(
        description="A realistic social/cultural scenario the student might face, grounded in their traits."
    )
    smart_response_strategy: str = Field(
        description="A healthy, practical strategy for handling this specific scenario."
    )


class CultureShockResult(BaseModel):
    """Structured output for `evaluate_culture_shock`. Passed as
    `response_schema` with `response_mime_type="application/json"` and
    validated against this shape on our side -- same posture as every
    other Gemini-backed service here."""

    cultural_differences_summary: str = Field(
        description="A brief, teen-friendly explanation of common social/cultural differences newcomers "
        "encounter in the target country."
    )
    scenario_simulations: list[CultureShockScenario] = Field(
        description="3 realistic scenarios grounded in the student's own stated traits."
    )
    coping_mechanisms: list[str] = Field(
        description="3 healthy ways to deal with homesickness or isolation abroad."
    )


_CULTURE_SHOCK_SYSTEM_PROMPT = """\
You are a cross-cultural psychologist and teen transition coach, \
psychologically preparing a high school student for the culture shock \
that often comes with moving abroad at a young age.

You will be given the student's target country and a list of their own \
personality traits.

NOTE: you are not given the student's home country or cultural \
background -- speak in general, honest terms about social/cultural \
differences that students from a WIDE VARIETY of backgrounds commonly \
encounter when moving to the target country (e.g. classroom \
participation norms, personal space and social distance conventions, \
directness of communication, punctuality expectations), rather than \
assuming any specific country of origin.

Your job:
1. Write a `cultural_differences_summary` -- brief and teen-friendly, \
covering the biggest, most commonly-encountered social differences \
newcomers face in the target country.
2. Write exactly 3 `scenario_simulations`, each a realistic social \
situation genuinely grounded in the student's OWN stated traits (e.g. \
an introverted student paired with an extroverted roommate, a student \
used to strict rules encountering a more relaxed classroom, a student \
used to indirect communication encountering blunt directness) -- not \
generic scenarios disconnected from what they actually told you about \
themselves. Each needs a `scenario_description` and a healthy, \
practical `smart_response_strategy`.
3. Write exactly 3 `coping_mechanisms` -- healthy, genuinely actionable \
ways to deal with homesickness or isolation abroad (e.g. maintaining \
regular contact with home, building one close local connection, joining \
a structured activity). Never suggest coping strategies involving \
substances, self-isolation, or anything psychologically unhealthy.

Rules:
- Ground scenarios specifically in the student's own stated traits, not \
generic "culture shock" filler.
- Never assume a specific home country/culture the student is coming \
from -- speak in general terms suitable for a student from any \
background.
- Keep tone warm, reassuring, and psychologically healthy throughout -- \
this is meant to reduce anxiety about the transition, not amplify it.
"""


def _format_prompt(target_country: str, student_traits: list[str]) -> str:
    return f"Target country: {target_country}\nStudent traits: {', '.join(student_traits)}"


class CultureShockEvaluatorGateway:
    """Async wrapper around Gemini-based academic culture shock
    evaluation via the `google-genai` SDK -- same package/model-
    substitution rationale as every other Gemini-backed service in this
    codebase (gemini-1.5-flash / google-generativeai are both stale; see
    the README note on this from Step 3.4).

    Disclosed gap: the ask's own output description references "the
    biggest social differences between their background and the
    target_country," but the literal method signature only takes
    `target_country` and `student_traits` -- no home-country/background
    field exists. Rather than adding an unrequested parameter or
    inventing an assumed origin, this resolves it by having the system
    prompt speak in general terms about differences newcomers from a
    wide variety of backgrounds commonly encounter, which keeps the
    literal signature exactly as specified."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def evaluate_culture_shock(
        self, target_country: str, student_traits: list[str]
    ) -> CultureShockResult:
        """Generate realistic cultural/social scenarios and healthy
        coping mechanisms to psychologically prepare a teen for moving
        to a target country, based on their own personality traits."""
        client = self._client()

        prompt = _format_prompt(target_country, student_traits)

        response = await client.aio.models.generate_content(
            model=settings.gemini_culture_shock_model,
            contents=[_CULTURE_SHOCK_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=CultureShockResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for culture shock evaluation")

        return CultureShockResult.model_validate_json(response.text)


culture_shock_evaluator_gateway = CultureShockEvaluatorGateway()
