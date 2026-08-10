import logging

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, Field
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings

logger = logging.getLogger(__name__)

# Same retry posture as vision.py: `errors.ClientError` (429s) and
# `errors.ServerError` (5xx) both inherit from `errors.APIError` in the
# current `google-genai` SDK, so catching the base class covers both in
# one policy.
_retry_gemini_call = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(genai_errors.APIError),
)


class EssayGradeResult(BaseModel):
    """Structured output Gemini is asked to produce when grading a
    motivation letter against a specific opportunity. Passed as
    `response_schema` with `response_mime_type="application/json"` --
    the model is instructed to return JSON matching this shape, and the
    response is validated against it on our side rather than trusted
    blindly (same posture as `ExtractedDocumentSchema` in vision.py)."""

    score: int = Field(
        ge=0,
        le=100,
        description="Overall quality of the letter for this specific "
        "opportunity, out of 100.",
    )
    grammar_and_spelling_issues: list[str] = Field(
        default_factory=list,
        description="Concrete grammar, spelling, or phrasing issues found "
        "in the letter, quoting or describing the specific problem. Empty "
        "list if none found -- do not invent issues to fill this out.",
    )
    content_feedback: str = Field(
        description="What is missing or weak in the letter's content "
        "relative to what this specific opportunity is looking for -- "
        "e.g. unaddressed eligibility points, generic claims not backed "
        "by specifics, missing motivation tied to the opportunity."
    )
    improvement_suggestions: list[str] = Field(
        default_factory=list,
        description="Concrete, actionable advice to make the letter more "
        "persuasive for this opportunity -- not generic writing tips that "
        "could apply to any letter.",
    )


_GRADING_SYSTEM_PROMPT = """\
You are an admissions/selection-committee reviewer grading a candidate's \
motivation letter against a specific opportunity's stated context.

Rules:
- Score honestly out of 100 based on how well this letter, as written, \
would persuade a reviewer for THIS opportunity -- a generic but \
well-written letter that ignores the opportunity's specifics should not \
score as high as one that engages with them directly.
- `grammar_and_spelling_issues` must list only issues actually present in \
the text. If the letter is clean, return an empty list -- do not invent \
problems to appear thorough.
- `content_feedback` must be grounded in what the opportunity's \
description/eligibility criteria actually say and what the letter actually \
contains -- do not fault the letter for omitting something the opportunity \
never asked for.
- `improvement_suggestions` must be concrete and actionable for this \
specific letter and opportunity, not generic "be more confident" advice.
"""


def _format_opportunity(opportunity_details: dict) -> str:
    lines = [f"Title: {opportunity_details.get('title', 'Unknown')}"]
    if opportunity_details.get("description"):
        lines.append(f"Description: {opportunity_details['description']}")
    if opportunity_details.get("eligibility_criteria"):
        lines.append(f"Eligibility criteria: {opportunity_details['eligibility_criteria']}")
    if opportunity_details.get("funding_details"):
        lines.append(f"Funding details: {opportunity_details['funding_details']}")
    return "\n".join(lines)


class GraderGateway:
    """Async wrapper around Gemini text grading via the current (2024+)
    unified `google-genai` SDK.

    Deliberately NOT `google-generativeai` (the package literally named
    in the ask) and NOT `gemini-1.5-flash` (the model literally named):
    both were already established as stale in Step 3.4 -- Google sunset
    `google-generativeai` on 2025-08-31 in favor of `google-genai`
    (`from google import genai`), and `gemini-1.5-flash` has since been
    superseded by `gemini-2.5-flash`. This reuses that same, already-
    verified substitution rather than re-deriving it.
    """

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as AIGateway
        # (app/services/ai.py) and VisionGateway (app/services/vision.py):
        # this may be called from contexts with different event-loop
        # lifecycles, so a client isn't assumed safe to share across them.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def grade_motivation_letter(
        self, letter_text: str, opportunity_details: dict
    ) -> EssayGradeResult:
        """Grade `letter_text` against `opportunity_details` (expects
        title/description/eligibility_criteria/funding_details keys)."""
        client = self._client()

        prompt = (
            f"{_format_opportunity(opportunity_details)}\n\n"
            f"Candidate's motivation letter:\n{letter_text}"
        )

        response = await client.aio.models.generate_content(
            model=settings.gemini_grading_model,
            contents=[_GRADING_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=EssayGradeResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for letter grading")

        # Validated via Pydantic against the response text rather than
        # trusting an SDK convenience attribute -- same posture as
        # vision.py's extract_document.
        return EssayGradeResult.model_validate_json(response.text)


grader_gateway = GraderGateway()
