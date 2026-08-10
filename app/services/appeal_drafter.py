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


class AppealDraftResult(BaseModel):
    """Structured output for `draft_appeal_letter`. Passed as
    `response_schema` with `response_mime_type="application/json"` and
    validated against this shape on our side -- same posture as every
    other Gemini-backed service here.

    `suggested_approach` is a plain `str`, not a hard `Literal` -- the
    ask phrased it as "whether they should politely appeal, ask for
    feedback, or just thank the committee," which reads as illustrative
    of the range of reasonable responses, not necessarily an exhaustive
    enum -- same "e.g. means non-exhaustive" reasoning already applied to
    `peer_review.py`'s `final_decision` and `trend_predictor.py`'s
    `confidence_level`.
    """

    rejection_reason_analysis: str = Field(
        description="A clear explanation of why the candidate was likely "
        "rejected, based on what the rejection text actually states or "
        "implies."
    )
    suggested_approach: str = Field(
        description="The recommended course of action given this specific "
        "rejection -- typically along the lines of politely appealing, "
        "asking for feedback, or simply thanking the committee -- chosen "
        "based on whether the rejection reads as final/procedural versus "
        "genuinely open to reconsideration."
    )
    letter_body: str = Field(
        description="The drafted email/letter matching the "
        "suggested_approach -- ready to send with minimal editing."
    )
    next_steps_advice: list[str] = Field(
        default_factory=list,
        description="2-3 concrete suggestions for improving the "
        "candidate's profile ahead of the next application cycle.",
    )


_APPEAL_DRAFTER_SYSTEM_PROMPT = """\
You are a diplomatic academic advisor and appeals expert, helping a student \
respond professionally to a rejection they received for a scholarship, \
internship, or program.

You will be given the actual rejection email/letter text the student \
received, and the student's own profile.

Your job:
1. Read the rejection text and identify the stated or implicit reason for \
rejection -- based only on what the text actually says or strongly \
implies, not speculation dressed up as certainty.
2. Decide the most sensible course of action for THIS specific rejection: \
does it read as final and procedural (in which case a gracious thank-you, \
or a polite request for feedback, is more appropriate than an appeal), or \
does it leave room for genuine reconsideration or clarification (in which \
case a respectful, substantive appeal may be worthwhile)? Do not default \
to "appeal" if the rejection doesn't actually warrant one -- pushing an \
appeal where none is warranted can hurt the student's standing, not help it.
3. Draft `letter_body` matching the chosen approach -- polite, \
professional, and diplomatic throughout, regardless of which approach was \
chosen.
4. Give 2-3 concrete `next_steps_advice` items for the next cycle, tied to \
the actual rejection reason and the student's actual profile gaps.

CRITICAL rules:
- The letter must be genuinely diplomatic and respectful -- never \
defensive, entitled, or adversarial in tone, even if the rejection seems \
unfair. A poorly-toned appeal can permanently damage the relationship with \
that institution.
- Do not fabricate specific facts, achievements, or credentials about the \
student that weren't in their profile -- ground the letter only in what \
was actually given.
- Do not invent specific claims about the rejection ("you said my GPA was \
too low") that the rejection text didn't actually make -- if the reason is \
genuinely unclear from the text, say so honestly in \
`rejection_reason_analysis` rather than inventing a specific reason to \
sound more certain.
- Keep the letter concise and professional -- a few short paragraphs, not \
an exhaustive essay.
"""


def _format_student_profile(student_profile: dict) -> str:
    lines = []
    for label, key in (
        ("Major", "major"),
        ("Degree level", "degree_level"),
        ("GPA", "gpa"),
        ("Graduation year", "graduation_year"),
        ("Skills", "skills"),
        ("Target country", "target_country"),
    ):
        value = student_profile.get(key)
        if value:
            lines.append(f"{label}: {value}")
    return "\n".join(lines) if lines else "(No profile details provided.)"


class AppealDrafterGateway:
    """Async wrapper around Gemini-based rejection appeal drafting via
    the `google-genai` SDK -- same package/model-substitution rationale
    as every other Gemini-backed service in this codebase
    (gemini-1.5-flash / google-generativeai are both stale; see the
    README note on this from Step 3.4)."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def draft_appeal_letter(
        self, rejection_text: str, student_profile: dict
    ) -> AppealDraftResult:
        """Analyze `rejection_text` and draft an appropriate follow-up
        letter, informed by `student_profile`."""
        client = self._client()

        prompt = (
            f"Rejection text received:\n{rejection_text}\n\n"
            f"Student profile:\n{_format_student_profile(student_profile)}"
        )

        response = await client.aio.models.generate_content(
            model=settings.gemini_appeal_drafter_model,
            contents=[_APPEAL_DRAFTER_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=AppealDraftResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for appeal letter drafting")

        return AppealDraftResult.model_validate_json(response.text)


appeal_drafter_gateway = AppealDrafterGateway()
