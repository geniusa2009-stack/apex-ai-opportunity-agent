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


class ResearchOutlineResult(BaseModel):
    """Structured output for `generate_research_outline`. Passed as
    `response_schema` with `response_mime_type="application/json"` and
    validated against this shape on our side -- same posture as every
    other Gemini-backed service here."""

    academic_title: str = Field(description="A formal, catchy title for the research.")
    research_question: str = Field(description="A clear, focused question the research will answer.")
    suggested_methodology: str = Field(
        description='How a high schooler can actually research this, e.g. "Create a Google Form '
        'survey to test peer habits."'
    )
    literature_search_keywords: list[str] = Field(
        description="3-4 academic keywords to search on Google Scholar."
    )
    step_by_step_execution: list[str] = Field(
        description="A chronological list of 4 manageable steps to complete the paper."
    )


_RESEARCH_MENTOR_SYSTEM_PROMPT = """\
You are an encouraging academic research mentor for teenagers, helping \
a high school student turn a raw research idea into a structured, \
achievable mini-research outline -- and, in the process, teaching them \
the basics of academic research methodology. Top-tier universities \
highly value independent research from applicants, but high schoolers \
typically have no formal training in research methodology.

You will be given the student's topic idea and their target academic \
field.

Your job:
1. Write an `academic_title` -- formal and catchy, the kind of title \
that would appear on a real (small-scale) research paper.
2. Write a `research_question` -- clear, focused, and genuinely \
answerable through the methodology you'll propose (not too broad to \
ever be answered by a high schooler, not so narrow it's trivial).
3. Write `suggested_methodology` -- a brief, concrete explanation of how \
a high schooler can ACTUALLY carry out this research with resources \
genuinely available to them (e.g. an online survey via Google Forms, \
analysis of a public dataset, a structured literature review, \
interviews with willing local participants) -- NEVER propose \
methodology requiring expensive lab equipment, IRB-level human subjects \
protocols, proprietary datasets, or resources no high schooler could \
realistically access.
4. List 3-4 `literature_search_keywords` -- genuine academic search \
terms a student could actually type into Google Scholar to find real, \
relevant prior research on this topic.
5. Write a `step_by_step_execution` -- exactly 4 chronological, \
manageable steps a high schooler can realistically follow from start to \
finish to complete this mini-research project.

Rules:
- Every proposed methodology must be something a real high schooler, \
with no lab access, no funding, and no institutional research \
affiliation, could actually execute -- if in doubt, simplify further \
rather than propose something unrealistic.
- Keep the tone encouraging and confidence-building -- many students \
feel intimidated by "research," and the goal is to make it feel \
achievable.
- Ground the research question and methodology genuinely in the \
student's own stated topic idea and field, not a generic template.
"""


def _format_prompt(topic_idea: str, target_field: str) -> str:
    return f"Topic idea: {topic_idea}\nTarget field: {target_field}"


class ResearchMentorGateway:
    """Async wrapper around Gemini-based high school research outline
    generation via the `google-genai` SDK -- same package/model-
    substitution rationale as every other Gemini-backed service in this
    codebase (gemini-1.5-flash / google-generativeai are both stale; see
    the README note on this from Step 3.4)."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def generate_research_outline(
        self, topic_idea: str, target_field: str
    ) -> ResearchOutlineResult:
        """Turn a high school student's raw research idea into a
        structured, achievable mini-research outline with a realistic
        methodology."""
        client = self._client()

        prompt = _format_prompt(topic_idea, target_field)

        response = await client.aio.models.generate_content(
            model=settings.gemini_research_mentor_model,
            contents=[_RESEARCH_MENTOR_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ResearchOutlineResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for research outline generation")

        return ResearchOutlineResult.model_validate_json(response.text)


research_mentor_gateway = ResearchMentorGateway()
