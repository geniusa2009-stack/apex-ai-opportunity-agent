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


class SafeAlumniMessageResult(BaseModel):
    """Structured output for `draft_safe_message`. Passed as
    `response_schema` with `response_mime_type="application/json"` and
    validated against this shape on our side -- same posture as every
    other Gemini-backed service here.

    `networking_tips` stays a single flat list (per the ask's own "a
    list of 2 Do's and 1 Don't" phrasing) rather than being split into
    separate `dos`/`donts` fields -- the system prompt instructs each
    entry to be prefixed with "Do:" or "Don't:" so the distinction is
    still visible to the student despite the flat structure.
    """

    safety_and_etiquette_review: str = Field(
        description="A brief note on why certain questions might have been rephrased to be more polite/safe."
    )
    message_subject: str = Field(description="A catchy but professional subject line.")
    safe_message_draft: str = Field(
        description="The exact text the student can copy and send, combining their questions professionally."
    )
    networking_tips: list[str] = Field(
        description="2 'Do' tips and 1 'Don't' tip for waiting for a reply and handling rejection, "
        "each prefixed with 'Do:' or 'Don't:'."
    )


_SAFE_ALUMNI_CONNECTOR_SYSTEM_PROMPT = """\
You are a strict professional communication coach for teenagers, \
helping a high school student draft a networking message (for LinkedIn \
or email) to a past scholarship winner (an alumnus/alumna) they'd like \
to learn from. Teenagers reaching out to professionals often overshare, \
sound demanding, or unintentionally cross boundaries -- your job is to \
prevent that.

You will be given the scholarship name, a brief summary of the alumnus/\
alumna's profile, and the student's raw questions.

Your job:
1. Write a `safety_and_etiquette_review` -- a brief, honest note \
explaining how and why you rephrased any of the student's raw questions \
to be more polite, appropriately scoped, or safer (e.g. removing an \
overly personal or demanding phrasing, or a question that assumes too \
much of the alumnus's time).
2. Write a `message_subject` -- catchy but genuinely professional, \
appropriate for a first-contact networking message.
3. Write a `safe_message_draft` -- the exact, ready-to-send message \
text, combining the student's actual questions into one polite, \
concise, professional message. This message must:
   - NEVER include or request any sensitive personal data (the \
student's or the alumnus's home address, phone number, financial \
details, exact date of birth, school name/location details that could \
enable someone to locate a minor in person, or similar) -- keep contact \
requests limited to the platform being used (e.g. "happy to connect \
here on LinkedIn" or "reply whenever convenient by email").
   - Be respectful of the alumnus's time and boundaries -- no demanding \
tone, no assumption of a guaranteed reply, no excessive length.
   - Sound like a genuine, courteous teenager, not an overly formal \
corporate email or an overly casual text message.
4. Write `networking_tips` -- exactly 2 "Do" tips and 1 "Don't" tip \
about waiting for a reply and handling rejection or silence \
gracefully, each entry prefixed with "Do:" or "Don't:" so the type is \
clear.

Rules:
- Never include sensitive personal data (the student's or the \
alumnus's) anywhere in the drafted message -- this is a strict, \
non-negotiable safety rule given the student is a minor communicating \
with someone they don't know personally.
- Keep the message genuinely usable as-is -- the student should be able \
to copy `safe_message_draft` and send it without further editing.
- Keep tone warm but professional throughout.
"""


def _format_prompt(
    scholarship_name: str, alumni_profile_summary: str, student_questions: list[str]
) -> str:
    return (
        f"Scholarship name: {scholarship_name}\n"
        f"Alumni profile summary: {alumni_profile_summary}\n"
        f"Student questions: {', '.join(student_questions)}"
    )


class SafeAlumniConnectorGateway:
    """Async wrapper around Gemini-based safe teen-to-alumni networking
    message drafting via the `google-genai` SDK -- same package/model-
    substitution rationale as every other Gemini-backed service in this
    codebase (gemini-1.5-flash / google-generativeai are both stale; see
    the README note on this from Step 3.4).

    Distinct from `alumni.py`/`alumni_matchmaker.py` (Advanced Feature
    8, Step 3.22) -- that feature MATCHES a student with a suitable
    alumnus from a real DB-backed candidate pool; this feature instead
    drafts a SAFE OUTREACH MESSAGE to an alumnus the student has already
    identified. Different files, different router prefixes
    (`/match-alumni` vs. `/alumni-connector`), no overlap."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def draft_safe_message(
        self, scholarship_name: str, alumni_profile_summary: str, student_questions: list[str]
    ) -> SafeAlumniMessageResult:
        """Draft a respectful, safe, professional networking message a
        teen can send to a scholarship alumnus, based on the
        scholarship, the alumnus's profile summary, and the student's
        raw questions."""
        client = self._client()

        prompt = _format_prompt(scholarship_name, alumni_profile_summary, student_questions)

        response = await client.aio.models.generate_content(
            model=settings.gemini_alumni_connector_model,
            contents=[_SAFE_ALUMNI_CONNECTOR_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=SafeAlumniMessageResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for safe alumni message drafting")

        return SafeAlumniMessageResult.model_validate_json(response.text)


safe_alumni_connector_gateway = SafeAlumniConnectorGateway()
