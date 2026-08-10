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


class ProbabilityResult(BaseModel):
    """Structured output for `calculate_acceptance_probability`. Passed as
    `response_schema` with `response_mime_type="application/json"` and
    validated against this shape on our side -- same posture as every
    other Gemini-backed service here.

    `probability_score` is "higher is better" (an estimated match
    strength), same direction as the grader's `score` and skill_gap's
    `overall_fit_score` -- unlike devils_advocate.py's deliberately
    inverted `vulnerability_score`, there's no direction flip to flag
    here.
    """

    probability_score: int = Field(
        ge=0,
        le=100,
        description="0-100 estimated acceptance/match strength. Higher is "
        "better, matching every other 'score' field in this project "
        "except devils_advocate.py's vulnerability_score.",
    )
    key_strengths: list[str] = Field(
        default_factory=list,
        description="Concrete areas where the student's profile matches "
        "or exceeds the opportunity's requirements/preferences.",
    )
    key_weaknesses: list[str] = Field(
        default_factory=list,
        description="Concrete gaps between the student's profile and the "
        "opportunity's requirements, e.g. a GPA below a stated minimum, "
        "a missing language test score, or a missing technical skill "
        "the opportunity asks for.",
    )
    action_plan: list[str] = Field(
        default_factory=list,
        description="Highly actionable, concrete steps the student could "
        "take to raise the probability score, e.g. 'Complete a verified "
        "SQL course to bridge the database skills gap' -- not vague "
        "advice like 'improve your profile.'",
    )


_PROBABILITY_SYSTEM_PROMPT = """\
You are an experienced, pragmatic academic admissions officer estimating a \
student's realistic chances of acceptance into a specific opportunity \
(scholarship, internship, volunteer program, or course), based on their \
profile against that opportunity's stated requirements and preferences.

You will be given the student's profile and the opportunity's details \
(description, eligibility criteria, funding details). Your job:

1. Identify the opportunity's strict/hard requirements (things that must \
be met) versus soft preferences (things that help but aren't dealbreakers) \
-- based only on what the opportunity's own text actually states or \
strongly implies. Do not invent a requirement (e.g. a minimum GPA, a \
specific test score threshold, a specific skill) that the opportunity's \
text didn't actually mention.
2. Compare the student's actual profile data against those requirements \
and preferences, honestly and pragmatically -- a realistic, not \
inflated, estimate. Do not invent a qualification, credential, or skill \
for the student that wasn't actually given in their profile.
3. Identify concrete strengths (real matches or areas the student \
exceeds what's asked) and concrete weaknesses (real, specific gaps).
4. Give a highly actionable, concrete `action_plan` -- specific enough \
that the student could act on each item directly (e.g. "Complete a \
verified SQL course to bridge the database skills gap" rather than \
"improve your technical skills").

Rules:
- If the student's profile is missing a data point the opportunity seems \
to care about (e.g. no language test score on record, no GPA on record), \
treat that as a genuine weakness/uncertainty to flag -- do not assume a \
value that wasn't given, and do not silently ignore the gap either.
- Be pragmatic, not falsely encouraging or falsely harsh -- the goal is a \
realistic estimate the student can actually act on, not a motivational \
speech or an unnecessarily discouraging one.
- `key_strengths` and `key_weaknesses` must be specific and grounded in \
the actual profile/opportunity data given, not generic statements that \
could apply to any student or any opportunity.
"""


def _format_student_profile(student_profile: dict) -> str:
    lines = []
    for label, key in (
        ("Major", "major"),
        ("Degree level", "degree_level"),
        ("GPA", "gpa"),
        ("Graduation year", "graduation_year"),
        ("Skills", "skills"),
    ):
        value = student_profile.get(key)
        if value:
            lines.append(f"{label}: {value}")
    return "\n".join(lines) if lines else "(No profile details provided.)"


def _format_opportunity(opportunity_details: dict) -> str:
    lines = [f"Title: {opportunity_details.get('title', 'Unknown')}"]
    if opportunity_details.get("type"):
        lines.append(f"Type: {opportunity_details['type']}")
    if opportunity_details.get("description"):
        lines.append(f"Description: {opportunity_details['description']}")
    if opportunity_details.get("eligibility_criteria"):
        lines.append(f"Eligibility criteria: {opportunity_details['eligibility_criteria']}")
    if opportunity_details.get("funding_details"):
        lines.append(f"Funding details: {opportunity_details['funding_details']}")
    return "\n".join(lines)


class ProbabilityEngineGateway:
    """Async wrapper around Gemini-based acceptance probability estimation
    via the `google-genai` SDK -- same package/model-substitution
    rationale as every other Gemini-backed service in this codebase
    (gemini-1.5-flash / google-generativeai are both stale; see the
    README note on this from Step 3.4)."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def calculate_acceptance_probability(
        self, student_profile: dict, opportunity_details: dict
    ) -> ProbabilityResult:
        """Estimate `student_profile`'s acceptance probability against
        `opportunity_details`, with concrete strengths, weaknesses, and
        an action plan."""
        client = self._client()

        prompt = (
            f"Student profile:\n{_format_student_profile(student_profile)}\n\n"
            f"Opportunity:\n{_format_opportunity(opportunity_details)}"
        )

        response = await client.aio.models.generate_content(
            model=settings.gemini_probability_model,
            contents=[_PROBABILITY_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ProbabilityResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for acceptance probability calculation")

        return ProbabilityResult.model_validate_json(response.text)


probability_engine_gateway = ProbabilityEngineGateway()
