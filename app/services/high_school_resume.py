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


class ExtracurricularEntry(BaseModel):
    role_title: str
    professional_bullets: list[str] = Field(
        description="2-3 upgraded, action-verb-led bullet points describing this club/sport."
    )


class AcademicProjectEntry(BaseModel):
    project_name: str
    professional_bullets: list[str] = Field(
        description="Upgraded, action-verb-led bullet points describing this school project."
    )


class HighSchoolResumeResult(BaseModel):
    """Structured output for `build_resume`. Passed as `response_schema`
    with `response_mime_type="application/json"` and validated against
    this shape on our side -- same posture as every other Gemini-backed
    service here."""

    professional_summary: str = Field(description="A strong 2-sentence summary of the student's profile.")
    extracurricular_experience: list[ExtracurricularEntry] = Field(
        description="Clubs/sports translated into professional role_title + professional_bullets entries."
    )
    academic_projects: list[AcademicProjectEntry] = Field(
        description="School projects translated into professional project_name + professional_bullets entries."
    )
    skills_and_hobbies: list[str] = Field(
        description="A refined list of skills derived from the student's inputs."
    )


_RESUME_BUILDER_SYSTEM_PROMPT = """\
You are a college admissions resume expert, helping a high school \
student -- who has no professional work experience -- turn their school \
projects, clubs, sports, awards, and hobbies into a polished, \
professional resume suitable for college and scholarship applications.

You will be given the student's projects, clubs, awards, and hobbies.

Your job:
1. Write a `professional_summary` -- exactly 2 sentences, strong and \
specific to this student's actual activities, not generic filler.
2. Turn each club/sport into an `extracurricular_experience` entry with \
a professional `role_title` and 2-3 `professional_bullets` -- elevate \
the language with strong action verbs (e.g. "Spearheaded", \
"Collaborated", "Designed", "Organized", "Led") and quantify impact \
where the student's input plausibly supports it, but do NOT invent \
numbers, titles, or accomplishments the student didn't actually mention \
-- elevate the phrasing of what they told you, don't fabricate new \
achievements.
3. Turn each project into an `academic_projects` entry with a \
`project_name` and `professional_bullets`, same elevation approach and \
same anti-fabrication rule.
4. Write `skills_and_hobbies` -- a refined, professional-sounding list \
of skills genuinely derivable from the student's stated activities and \
hobbies (e.g. "Python programming" from a coding club, "Public \
speaking" from debate team), not invented skills unconnected to \
anything they told you.
5. Weave the student's stated `awards` into the professional_summary \
and/or the relevant extracurricular/academic bullets as credibility \
signals, even though awards don't have their own dedicated output \
section -- do not drop this information.

Rules:
- Elevate language, don't fabricate -- every bullet must be a truthful, \
polished restatement of something the student actually told you, never \
an invented achievement, title, number, or award.
- Avoid language so inflated it would sound obviously fake or \
implausible for a high schooler -- the goal is professional and \
credible, not exaggerated.
- Keep the overall tone confident and admissions-appropriate.
"""


def _format_prompt(
    projects: list[str], clubs: list[str], awards: list[str], hobbies: list[str]
) -> str:
    return (
        f"Projects: {', '.join(projects) if projects else '(none provided)'}\n"
        f"Clubs: {', '.join(clubs) if clubs else '(none provided)'}\n"
        f"Awards: {', '.join(awards) if awards else '(none provided)'}\n"
        f"Hobbies: {', '.join(hobbies) if hobbies else '(none provided)'}"
    )


class HighSchoolResumeGateway:
    """Async wrapper around Gemini-based high school resume building via
    the `google-genai` SDK -- same package/model-substitution rationale
    as every other Gemini-backed service in this codebase
    (gemini-1.5-flash / google-generativeai are both stale; see the
    README note on this from Step 3.4)."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def build_resume(
        self, projects: list[str], clubs: list[str], awards: list[str], hobbies: list[str]
    ) -> HighSchoolResumeResult:
        """Translate a high school student's projects, clubs, awards, and
        hobbies into a professional, action-oriented resume."""
        client = self._client()

        prompt = _format_prompt(projects, clubs, awards, hobbies)

        response = await client.aio.models.generate_content(
            model=settings.gemini_high_school_resume_model,
            contents=[_RESUME_BUILDER_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=HighSchoolResumeResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for high school resume building")

        return HighSchoolResumeResult.model_validate_json(response.text)


high_school_resume_gateway = HighSchoolResumeGateway()
