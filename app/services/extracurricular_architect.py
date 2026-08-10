import logging
from typing import Literal

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


class RecommendedProject(BaseModel):
    project_name: str
    description: str = Field(description="What the project actually is and why it fits this student.")
    # Bracket notation "[High/Medium]" in the ask (not prefixed with
    # "e.g.") reads as an actual two-value enum, not an illustrative
    # example -- unlike the "e.g." fields elsewhere in this project
    # (peer_review.py's final_decision, trend_predictor.py's
    # confidence_level, appeal_drafter.py's suggested_approach), so this
    # one IS hard-constrained to a `Literal`.
    perceived_impact_level: Literal["High", "Medium"]
    step_by_step_execution: list[str] = Field(
        default_factory=list, description="Concrete steps to actually start and complete this project."
    )


class ExtracurricularRoadmapResult(BaseModel):
    """Structured output for `generate_extracurricular_roadmap`. Passed
    as `response_schema` with `response_mime_type="application/json"`
    and validated against this shape on our side -- same posture as
    every other Gemini-backed service here."""

    roadmap_title: str = Field(description="A thematic title for this roadmap, e.g. 'The STEM Innovator Roadmap'.")
    profile_archetype: str = Field(
        description="A description of the student's competitive persona, "
        "e.g. 'The Community-Driven Scientist'."
    )
    recommended_projects: list[RecommendedProject] = Field(
        description="3-4 specific projects the student can start independently or at school."
    )
    volunteering_suggestions: list[str] = Field(
        default_factory=list,
        description="Realistic volunteer opportunities tailored to the student's stated interests.",
    )
    skills_to_acquire: list[str] = Field(
        default_factory=list,
        description="Hard/soft skills to learn, each paired with a suggested genuinely-free "
        "learning resource (e.g. a well-known free platform, not an invented or paid one).",
    )


_EXTRACURRICULAR_SYSTEM_PROMPT = """\
You are an elite admissions consultant specializing in holistic admissions \
for top-tier universities and international scholarships (Ivy League-caliber \
and equivalent globally), helping a high school or middle school student \
build a strategic extracurricular profile years before they apply.

You will be given the student's interests, current grade level, target \
undergraduate major, and how many hours per week they can realistically \
commit.

Your job:
1. Design a `roadmap_title` and `profile_archetype` that give this student \
a clear, compelling competitive identity -- not generic ("well-rounded \
student"), but specific to their actual interests and target major.
2. Recommend 3-4 `recommended_projects` the student can realistically start \
independently or through their school, each genuinely achievable within \
their stated `available_hours_per_week` -- not a project that actually \
requires far more time or resources than they have. Rate each project's \
`perceived_impact_level` honestly (High or Medium) based on how much it \
would actually strengthen a competitive application, and give concrete, \
sequential `step_by_step_execution` steps -- not vague advice.
3. Suggest realistic `volunteering_suggestions` tied to their actual \
interests -- opportunities that plausibly exist in most communities \
(e.g. local tutoring, community organizations, hospitals, libraries), not \
implausibly exclusive or invented programs.
4. List `skills_to_acquire` (hard and soft skills relevant to their target \
major), each paired with a genuinely free way to learn it -- well-known \
free platforms and resource TYPES (e.g. Khan Academy, freeCodeCamp, \
Coursera's free/audit courses, YouTube tutorials, public library \
resources) rather than an invented specific course/book/URL that may not \
actually exist, and never a paid resource described as free.

Rules:
- Every recommendation must be realistic and achievable for a real \
student at the stated grade level with the stated time budget -- not \
aspirational filler that sounds impressive but isn't actually doable.
- Do not invent a specific named program, competition, or organization \
that may not actually exist -- when suggesting a specific well-known \
program (e.g. a well-known science fair or well-known volunteer \
organization type), only reference genuinely widely-known ones; \
otherwise describe the TYPE of opportunity to look for locally rather \
than inventing a specific name.
- Keep advice age-appropriate and encouraging in tone.
"""


def _format_prompt(
    interests: list[str], grade_level: str, target_major: str, available_hours_per_week: int
) -> str:
    return (
        f"Interests: {', '.join(interests)}\n"
        f"Grade level: {grade_level}\n"
        f"Target undergraduate major: {target_major}\n"
        f"Available hours per week: {available_hours_per_week}"
    )


class ExtracurricularArchitectGateway:
    """Async wrapper around Gemini-based extracurricular roadmap
    generation via the `google-genai` SDK -- same package/model-
    substitution rationale as every other Gemini-backed service in this
    codebase (gemini-1.5-flash / google-generativeai are both stale; see
    the README note on this from Step 3.4)."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def generate_extracurricular_roadmap(
        self,
        interests: list[str],
        grade_level: str,
        target_major: str,
        available_hours_per_week: int,
    ) -> ExtracurricularRoadmapResult:
        """Generate a personalized extracurricular roadmap for a student
        with the given interests/grade level/target major/time budget."""
        client = self._client()

        prompt = _format_prompt(interests, grade_level, target_major, available_hours_per_week)

        response = await client.aio.models.generate_content(
            model=settings.gemini_extracurricular_model,
            contents=[_EXTRACURRICULAR_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ExtracurricularRoadmapResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for extracurricular roadmap generation")

        return ExtracurricularRoadmapResult.model_validate_json(response.text)


extracurricular_architect_gateway = ExtracurricularArchitectGateway()
