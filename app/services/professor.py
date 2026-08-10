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


class ProfessorEmailDraft(BaseModel):
    """Structured output for `draft_outreach_email`. Passed as
    `response_schema` with `response_mime_type="application/json"` and
    validated against this shape on our side -- same posture as every
    other Gemini-backed service here."""

    subject_line: str = Field(description="A specific, non-generic email subject line.")
    email_body: str = Field(
        description="The full cold-email body, personalized to the "
        "professor's actual research interests and the student's actual "
        "background -- ready to send with minimal editing."
    )
    strategic_advice: list[str] = Field(
        default_factory=list,
        description="Short, concrete tips for the student on sending "
        "this specific email well, e.g. 'Attach your transcript', "
        "'Read their 2024 paper on X before following up' -- not "
        "generic cold-emailing advice.",
    )


_PROFESSOR_OUTREACH_SYSTEM_PROMPT = """\
You are an expert academic strategist helping a student write a cold email \
to a professor, requesting supervision, a master's/PhD spot, or research \
funding.

You will be given the professor's research background and the student's own \
profile. Your job:

1. Analyze the professor's actual stated research interests -- identify what \
they specifically work on, not a generic guess at their field.
2. Draft a compelling, highly personalized email that explicitly connects \
the student's real background to the professor's actual research, rather \
than a generic template that could be sent to any professor.
3. Give the student concrete strategic advice for sending this specific email.

Rules:
- Use ONLY the information given about the student and the professor. Do not \
invent specific papers, projects, grants, dates, or credentials for either \
of them that weren't actually provided -- this email may really be sent, and \
a fabricated claim (e.g. citing a paper that doesn't exist, or a credential \
the student doesn't have) would seriously damage the student's credibility \
the moment the professor notices.
- If the professor's background mentions specific research topics, papers, \
or projects, reference them concretely and specifically. If it doesn't, \
keep the email's research framing general rather than inventing specifics \
to sound more informed than the student actually is.
- Avoid cliche cold-email openers ("I hope this email finds you well", "I am \
writing to express my interest in...") and empty flattery. Sound like a \
specific, informed student, not a mail-merge template.
- Keep the email concise -- a professor skims cold emails; aim for roughly \
150-300 words, not an exhaustive life story.
- `strategic_advice` must be concrete and specific to this email/professor/ \
student combination (e.g. referencing an actual paper or detail from the \
professor's background if one was given), not generic cold-emailing tips \
that could apply to anyone.
- Output `email_body` as plain text (paragraphs), not Markdown or HTML.
"""


class ProfessorGateway:
    """Async wrapper around Gemini professor-outreach email drafting via
    the `google-genai` SDK -- same package/model-substitution rationale
    as the other Gemini-backed services in this codebase
    (gemini-1.5-flash / google-generativeai are both stale; see the
    README note on this from Step 3.4)."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def draft_outreach_email(
        self, professor_background: str, student_profile: str
    ) -> ProfessorEmailDraft:
        """Draft a personalized cold-outreach email from a student to a
        professor, based on the professor's research background and the
        student's own profile."""
        client = self._client()

        prompt = (
            f"Professor's research background:\n{professor_background}\n\n"
            f"Student's profile:\n{student_profile}"
        )

        response = await client.aio.models.generate_content(
            model=settings.gemini_professor_model,
            contents=[_PROFESSOR_OUTREACH_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ProfessorEmailDraft,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for professor outreach email drafting")

        return ProfessorEmailDraft.model_validate_json(response.text)


professor_gateway = ProfessorGateway()
