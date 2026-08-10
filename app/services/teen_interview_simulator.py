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


class TeenInterviewQuestion(BaseModel):
    question_text: str
    what_we_really_want_to_know: str = Field(
        description="The hidden motive/trait the interviewer is actually probing for behind this question."
    )
    teen_friendly_strategy: str = Field(
        description="How to answer using simple, honest personal stories -- not corporate interview jargon."
    )


class TeenInterviewSimulationResult(BaseModel):
    """Structured output for `simulate_interview`. Passed as
    `response_schema` with `response_mime_type="application/json"` and
    validated against this shape on our side -- same posture as every
    other Gemini-backed service here."""

    interviewer_greeting: str = Field(
        description="A warm, encouraging opening statement to calm the student's nerves."
    )
    interview_questions: list[TeenInterviewQuestion] = Field(
        description="3-4 situational/behavioral questions targeting adaptability, cultural exchange, "
        "and the student's specific stated concerns."
    )
    confidence_booster: str = Field(description="A short, uplifting closing note.")


_TEEN_INTERVIEW_SYSTEM_PROMPT = """\
You are a friendly, empathetic scholarship interviewer who genuinely \
wants the student to succeed. You are interviewing a MINOR (under 18) \
for a scholarship. Interviews for minors focus far less on intense \
academic grilling and far more on maturity, adaptability, handling \
homesickness, and conflict resolution -- this is a warm conversation, \
not an interrogation.

You will be given the scholarship name and the student's specific \
concerns/anxieties about the interview or the experience ahead.

Your job:
1. Write an `interviewer_greeting` -- warm and genuinely calming, the \
kind of opening that would put a nervous teenager at ease immediately.
2. Write 3-4 `interview_questions`, each targeting adaptability, \
cultural exchange, and/or the student's specific stated concerns \
(directly address concerns like homesickness or language nervousness \
with a question genuinely built around that concern, not a generic \
question that ignores what they told you). For each question, explain \
`what_we_really_want_to_know` -- the real trait or quality the \
interviewer is actually assessing behind the surface-level question \
(e.g. a question about a disagreement with a friend is really testing \
conflict-resolution maturity) -- and give a `teen_friendly_strategy` \
for how to answer using a simple, honest personal story rather than \
rehearsed corporate-style interview language.
3. Write a `confidence_booster` -- a short, genuinely uplifting closing \
note that leaves the student feeling capable and less anxious than when \
they started.

Rules:
- Keep every question age-appropriate and warm -- this is explicitly \
NOT meant to feel like a hostile or high-pressure interrogation.
- Directly address the student's specific stated concerns rather than \
generic interview advice that ignores what they told you.
- Keep language simple and encouraging throughout, appropriate for a \
teenager who may be nervous about this process.
"""


def _format_prompt(scholarship_name: str, student_concerns: list[str]) -> str:
    return f"Scholarship name: {scholarship_name}\nStudent concerns: {', '.join(student_concerns)}"


class TeenInterviewSimulatorGateway:
    """Async wrapper around Gemini-based friendly teen scholarship
    interview simulation via the `google-genai` SDK -- same
    package/model-substitution rationale as every other Gemini-backed
    service in this codebase (gemini-1.5-flash / google-generativeai are
    both stale; see the README note on this from Step 3.4).

    Distinct from `interview.py` (Phase 3, general interview question
    generation) and `multi_agent_interview.py` (Legendary Feature 1,
    multi-persona group interview simulation) -- this service is
    specifically a single warm, minor-appropriate interviewer persona
    focused on the emotional/maturity dimensions unique to under-18
    scholarship candidates, not general interview prep or a competitive
    panel dynamic."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def simulate_interview(
        self, scholarship_name: str, student_concerns: list[str]
    ) -> TeenInterviewSimulationResult:
        """Simulate a warm, encouraging scholarship interview tailored to
        a teen's specific anxieties and target scholarship."""
        client = self._client()

        prompt = _format_prompt(scholarship_name, student_concerns)

        response = await client.aio.models.generate_content(
            model=settings.gemini_teen_interview_model,
            contents=[_TEEN_INTERVIEW_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=TeenInterviewSimulationResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for teen interview simulation")

        return TeenInterviewSimulationResult.model_validate_json(response.text)


teen_interview_simulator_gateway = TeenInterviewSimulatorGateway()
