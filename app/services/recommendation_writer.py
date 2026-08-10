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


class RecommendationLetterResult(BaseModel):
    """Structured output for `generate_recommendation_letter`. Passed as
    `response_schema` with `response_mime_type="application/json"` and
    validated against this shape on our side -- same posture as every
    other Gemini-backed service here."""

    letter_subject: str = Field(
        description="A formal subject/heading line for the letter, e.g. "
        "'Letter of Recommendation for [Student Name]'."
    )
    letter_body: str = Field(
        description="The full letter as plain text (paragraphs), including "
        "a formal salutation and sign-off -- ready to review and sign "
        "with minimal editing."
    )
    academic_tone_analysis: str = Field(
        description="A brief explanation of the key academic traits and "
        "strengths this specific letter emphasized, and why."
    )


_RECOMMENDATION_WRITER_SYSTEM_PROMPT = """\
You are drafting a Letter of Recommendation (LOR), written from the \
perspective of the referee themselves (a senior university professor or \
industry supervisor) on behalf of a student applying to a specific \
scholarship. In many regions, students draft their own LOR for the \
referee to review and sign -- so this letter must read as authentically \
written BY the referee, in their voice, not as a student describing \
themselves in the third person.

You will be given: the student's name, the referee's name and title, how \
long they've known each other and in what capacity, the student's key \
achievements, and the target scholarship.

Your job:
1. Write in the voice of the referee -- authoritative, highly \
professional, objective, and genuinely supportive. This is a serious \
endorsement, not generic praise.
2. Structure `letter_body` as a real formal letter: a formal salutation \
(e.g. "To the Admissions Committee,"), body paragraphs establishing the \
referee's relationship to the student and their basis for judgment, \
specific evidence of the student's strengths tied to the achievements \
given, a closing endorsement connecting the student's fit to the target \
scholarship, and a formal sign-off with the referee's name and title.
3. Give `letter_subject`: a formal heading line.
4. Give `academic_tone_analysis`: a short explanation of which specific \
academic/professional traits this letter emphasized and why they were \
chosen given the achievements provided.

CRITICAL rule -- this is a real document a real referee will sign and \
submit under their own name and professional reputation: use ONLY the \
facts actually given (relationship duration/context, key achievements, \
target scholarship). Do NOT invent specific grades, papers, publications, \
awards, projects, anecdotes, or dates that weren't provided. A fabricated \
specific claim in a signed recommendation letter is far more damaging than \
generic phrasing -- if you don't have a concrete detail, write \
authentically and warmly using only what was actually given, rather than \
inventing a specific-sounding but false one to fill the letter out.

Additional rules:
- Avoid generic, interchangeable phrasing that could describe any student \
("hardworking", "a pleasure to teach") without tying it back to the \
specific achievements given -- ground every claim of strength in the \
actual evidence provided.
- Keep the tone formal and academic throughout -- no casual language.
- Output `letter_body` as plain text (paragraphs), not Markdown or HTML.
"""


def _format_prompt(
    student_name: str,
    referee_name: str,
    referee_title: str,
    relationship_duration: str,
    key_achievements: str,
    target_scholarship: str,
) -> str:
    return (
        f"Student name: {student_name}\n"
        f"Referee name: {referee_name}\n"
        f"Referee title: {referee_title}\n"
        f"Relationship duration/context: {relationship_duration}\n"
        f"Student's key achievements: {key_achievements}\n"
        f"Target scholarship: {target_scholarship}"
    )


class RecommendationWriterGateway:
    """Async wrapper around Gemini-based recommendation letter drafting
    via the `google-genai` SDK -- same package/model-substitution
    rationale as every other Gemini-backed service in this codebase
    (gemini-1.5-flash / google-generativeai are both stale; see the
    README note on this from Step 3.4)."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def generate_recommendation_letter(
        self,
        student_name: str,
        referee_name: str,
        referee_title: str,
        relationship_duration: str,
        key_achievements: str,
        target_scholarship: str,
    ) -> RecommendationLetterResult:
        """Draft a Letter of Recommendation from `referee_name`
        (`referee_title`) on behalf of `student_name`, for
        `target_scholarship`, grounded only in `relationship_duration`
        and `key_achievements`."""
        client = self._client()

        prompt = _format_prompt(
            student_name,
            referee_name,
            referee_title,
            relationship_duration,
            key_achievements,
            target_scholarship,
        )

        response = await client.aio.models.generate_content(
            model=settings.gemini_recommendation_writer_model,
            contents=[_RECOMMENDATION_WRITER_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=RecommendationLetterResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for recommendation letter generation")

        return RecommendationLetterResult.model_validate_json(response.text)


recommendation_writer_gateway = RecommendationWriterGateway()
