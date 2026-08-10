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


class TeacherGuideResult(BaseModel):
    """Structured output for `generate_teacher_guide`. Passed as
    `response_schema` with `response_mime_type="application/json"` and
    validated against this shape on our side -- same posture as every
    other Gemini-backed service here."""

    cultural_context_note: str = Field(
        description="What universities in the student's target country value most in a recommendation letter, "
        "e.g. US values leadership, UK values academic depth."
    )
    letter_structure: list[str] = Field(
        description="A step-by-step outline of how the letter should be organized."
    )
    strengths_to_highlight: list[str] = Field(
        description="How the teacher can specifically frame the student's provided strengths "
        "in the context of the teacher's own subject."
    )
    sample_template: str = Field(
        description="A fill-in-the-blank professional draft the teacher can use or adapt, with clearly "
        "marked placeholders (e.g. [insert specific example]) for anything only the teacher actually knows."
    )


_TEACHER_GUIDE_SYSTEM_PROMPT = """\
You are an international university admissions director, explaining to \
a local high school teacher -- who may be unfamiliar with the specific \
formats and expectations of international university recommendation \
letters -- what makes a winning recommendation letter for their student.

You will be given the student's name, their target country for study, \
the teacher's subject, and the student's strengths as described by the \
student or school.

Your job:
1. Write a `cultural_context_note` -- a brief, concrete note on what \
universities in the student's target country specifically value most in \
a recommendation letter (e.g. US admissions tends to value leadership \
and personal impact narratives; UK admissions tends to value \
demonstrated academic depth and subject-specific intellectual \
curiosity; adapt this honestly to whatever country you're given rather \
than assuming every country wants the same thing).
2. Write a `letter_structure` -- a step-by-step outline of how the \
letter should be organized (e.g. opening context, specific anecdote, \
comparison to peers, closing endorsement), genuinely useful and \
specific rather than vague generic advice.
3. Write `strengths_to_highlight` -- concrete guidance on how the \
teacher can specifically frame the student's stated strengths through \
the lens of their own subject (e.g. how a described "curiosity" \
strength shows up specifically in a physics classroom vs. an English \
classroom) -- tailored to the actual teacher_subject given, not generic.
4. Write a `sample_template` -- a professional, fill-in-the-blank draft \
letter the teacher can use or adapt. Use the student's real name \
naturally throughout, but for anything you do not actually know (a \
specific anecdote, a specific grade or rank, the teacher's own name, \
specific dates), use a clearly marked placeholder like \
"[insert specific example of...]" rather than inventing a fake detail \
that could end up in a real, signed letter sent to a real institution.

Rules:
- This may become a REAL letter that gets signed by a real teacher and \
sent to real universities -- never invent a specific achievement, \
anecdote, grade, ranking, or fact about the student beyond what was \
actually provided in `student_strengths`. Use placeholders for anything \
you don't actually know, exactly as instructed above.
- Keep the tone professional, warm, and genuinely helpful to a teacher \
who may be doing this for the first time.
"""


def _format_prompt(
    student_name: str, target_country: str, teacher_subject: str, student_strengths: list[str]
) -> str:
    return (
        f"Student name: {student_name}\n"
        f"Target country: {target_country}\n"
        f"Teacher subject: {teacher_subject}\n"
        f"Student strengths: {', '.join(student_strengths)}"
    )


class TeacherGuideGateway:
    """Async wrapper around Gemini-based teacher recommendation-letter
    guide generation via the `google-genai` SDK -- same package/model-
    substitution rationale as every other Gemini-backed service in this
    codebase (gemini-1.5-flash / google-generativeai are both stale; see
    the README note on this from Step 3.4)."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def generate_teacher_guide(
        self,
        student_name: str,
        target_country: str,
        teacher_subject: str,
        student_strengths: list[str],
    ) -> TeacherGuideResult:
        """Generate a customized guide and fill-in-the-blank template to
        help a high school teacher write a strong international
        university recommendation letter for a student."""
        client = self._client()

        prompt = _format_prompt(student_name, target_country, teacher_subject, student_strengths)

        response = await client.aio.models.generate_content(
            model=settings.gemini_teacher_guide_model,
            contents=[_TEACHER_GUIDE_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=TeacherGuideResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for teacher guide generation")

        return TeacherGuideResult.model_validate_json(response.text)


teacher_guide_gateway = TeacherGuideGateway()
