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


class PolishResult(BaseModel):
    """Structured output for `polish_text`. Passed as `response_schema`
    with `response_mime_type="application/json"` and validated against
    this shape on our side -- same posture as every other Gemini-backed
    service here."""

    polished_text: str = Field(
        description="The rewritten text: grammatically correct, "
        "elevated to an academic/professional tone, with the same "
        "core meaning and intent as the original."
    )
    key_changes: list[str] = Field(
        default_factory=list,
        description="A short list of what was actually improved, e.g. "
        "'Elevated vocabulary', 'Fixed tense consistency', 'Removed "
        "run-on sentence in paragraph 2' -- specific to this text, not "
        "generic writing-tips boilerplate.",
    )


_POLISH_SYSTEM_PROMPT = """\
You are an academic writing editor. You are given a user's rough draft -- \
an essay paragraph, application answer, or email to a professor -- and \
your job is to refine it into polished, professional academic English.

Rules:
- Correct grammar, spelling, and punctuation errors.
- Improve vocabulary and sentence structure to an academic/professional \
standard -- but do not make it sound artificially stiff, overwrought, or \
inflated with jargon the original didn't call for.
- Preserve the original meaning and intent exactly. Do not add claims, \
facts, examples, or arguments the user didn't write, and do not remove \
substantive content -- this is an edit, not a rewrite of what the person \
is actually saying.
- Keep the original's basic structure (e.g. don't turn one paragraph into \
three, or merge multiple distinct points into one) unless the original's \
structure itself is a grammatical problem (e.g. a run-on sentence).
- `key_changes` must describe what was *actually* changed in *this* text \
-- not a generic list of things a polisher might do. If very little \
changed, keep the list short and specific (e.g. just "Fixed a subject-verb \
agreement error"), rather than padding it out.
"""


class PolisherGateway:
    """Async wrapper around Gemini grammar/tone polishing via the
    `google-genai` SDK -- same package/model-substitution rationale as
    the other Gemini-backed services in this codebase (gemini-1.5-flash
    / google-generativeai are both stale; see the README note on this
    from Step 3.4)."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def polish_text(self, raw_text: str) -> PolishResult:
        """Refine `raw_text` into polished academic/professional English,
        returning the rewritten text plus a short list of what changed."""
        client = self._client()

        prompt = f"Original text:\n{raw_text}"

        response = await client.aio.models.generate_content(
            model=settings.gemini_polisher_model,
            contents=[_POLISH_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=PolishResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for text polishing")

        return PolishResult.model_validate_json(response.text)


polisher_gateway = PolisherGateway()
