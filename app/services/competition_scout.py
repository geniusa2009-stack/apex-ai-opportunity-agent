import logging
from typing import Literal

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


class TopCompetition(BaseModel):
    competition_name: str
    # Bracket notation "[Beginner/Intermediate/Advanced]" in the ask (not
    # prefixed with "e.g.") reads as an actual three-value enum, not an
    # illustrative example -- same distinction applied to
    # extracurricular_architect.py's perceived_impact_level (Step 3.35).
    difficulty_level: Literal["Beginner", "Intermediate", "Advanced"]
    description: str
    # "[e.g., \"Qualifiers in March, Finals in May\"]" IS prefixed with
    # "e.g.," so this stays a plain, freeform string -- same posture as
    # peer_review.py's final_decision / trend_predictor.py's
    # confidence_level / appeal_drafter.py's suggested_approach. Real
    # competitions have wildly different timeline shapes (some are a
    # single exam day, some are multi-round over months), so hard-coding
    # a format here would be actively wrong for many of them.
    typical_timeline: str = Field(description='E.g. "Qualifiers in March, Finals in May".')
    winning_impact_on_scholarships: str = Field(
        description="How placing well in this specific competition tends to strengthen scholarship applications."
    )


class CompetitionScoutResult(BaseModel):
    """Structured output for `scout_competitions`. Passed as
    `response_schema` with `response_mime_type="application/json"` and
    validated against this shape on our side -- same posture as every
    other Gemini-backed service here."""

    scout_summary: str = Field(
        description="A brief, encouraging paragraph on why competing matters for this student's major."
    )
    top_competitions: list[TopCompetition] = Field(
        description="3-5 specific competitions genuinely suited to this student's age, major interest, and skill level."
    )
    preparation_plan: list[str] = Field(
        description="3 concrete, actionable steps to start preparing for the top recommended competition today."
    )


_COMPETITION_SCOUT_SYSTEM_PROMPT = """\
You are an elite talent scout for international academic competitions \
(science fairs like ISEF, Math/Physics/Chemistry/Informatics Olympiads, \
coding competitions, debate championships, and similar), helping a \
student find high-impact competitions that will strengthen their global \
scholarship and undergraduate admissions profile.

You will be given the student's age, academic major interest, and current \
skill level.

Your job:
1. Write a `scout_summary` -- a brief, encouraging paragraph on why \
competing in this field is crucial for a student aiming at their stated \
major, specific to that major rather than generic.
2. Recommend 3-5 `top_competitions` genuinely suited to the student's \
age, major interest, and current skill level. Only reference real, \
genuinely well-known competitions (e.g. ISEF, International Mathematical \
Olympiad and its national feeder olympiads, USACO/other well-known coding \
competitions, well-known debate/Model UN championships) that plausibly \
match the student's stated age and skill level -- do not recommend a \
competition the student's age or skill level would realistically exclude \
them from, and do not invent a competition name that does not actually \
exist. For each, give an honest `difficulty_level` (Beginner, \
Intermediate, or Advanced), a clear `description`, its `typical_timeline`, \
and a realistic explanation of its `winning_impact_on_scholarships`.
3. Write a `preparation_plan` of exactly 3 concrete, actionable steps the \
student can start today to prepare for the single top-recommended \
competition -- not vague advice.

Rules:
- Never invent a specific competition name, date, or eligibility rule \
that may not actually exist -- when unsure of a precise detail, describe \
it in general, honest terms rather than fabricating specifics.
- Keep recommendations realistic for the student's stated age and skill \
level -- do not recommend competitions that are implausibly advanced for \
a true beginner, or too introductory for an advanced student.
- Keep tone encouraging and age-appropriate.
"""


def _format_prompt(age: int, major_interest: str, current_skill_level: str) -> str:
    return (
        f"Age: {age}\n"
        f"Major interest: {major_interest}\n"
        f"Current skill level: {current_skill_level}"
    )


class CompetitionScoutGateway:
    """Async wrapper around Gemini-based competition scouting via the
    `google-genai` SDK -- same package/model-substitution rationale as
    every other Gemini-backed service in this codebase (gemini-1.5-flash
    / google-generativeai are both stale; see the README note on this
    from Step 3.4)."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def scout_competitions(
        self, age: int, major_interest: str, current_skill_level: str
    ) -> CompetitionScoutResult:
        """Match a student with high-impact academic competitions based
        on their age, major interest, and current skill level."""
        client = self._client()

        prompt = _format_prompt(age, major_interest, current_skill_level)

        response = await client.aio.models.generate_content(
            model=settings.gemini_competition_scout_model,
            contents=[_COMPETITION_SCOUT_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=CompetitionScoutResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for competition scouting")

        return CompetitionScoutResult.model_validate_json(response.text)


competition_scout_gateway = CompetitionScoutGateway()
