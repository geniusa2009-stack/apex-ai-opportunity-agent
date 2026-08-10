import logging
from datetime import date

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, Field, field_validator
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


class ExtractedDeadline(BaseModel):
    """Structured output for `extract_deadline`. Passed as
    `response_schema` with `response_mime_type="application/json"` and
    validated against this shape on our side -- same posture as every
    other Gemini-backed service here."""

    extracted_date: str | None = Field(
        default=None,
        description="The application deadline in ISO-8601 date format "
        "(YYYY-MM-DD), or null if no deadline is stated in the text.",
    )
    confidence_score: int = Field(
        ge=0,
        le=100,
        description="Confidence (0-100) that extracted_date is correct "
        "and genuinely refers to the application deadline (not some "
        "other date mentioned in the text, e.g. a start date or an "
        "founding/establishment year).",
    )
    original_text_reference: str | None = Field(
        default=None,
        description="The exact snippet of the source text the date was "
        "found in, quoted verbatim -- null if no date was found.",
    )

    @field_validator("extracted_date")
    @classmethod
    def _validate_iso_date(cls, value: str | None) -> str | None:
        # The model is instructed to return strict YYYY-MM-DD, but
        # nothing stops it from returning a slightly malformed string
        # (e.g. "August 2026" if it couldn't pin down a day, or a
        # non-ISO format). A malformed date silently propagating into a
        # reminder-scheduling feature later would be worse than just not
        # having one -- but hard-failing the whole request over a date-
        # format slip (when confidence_score and original_text_reference
        # might still be perfectly useful) is also the wrong tradeoff.
        # So: validate here, and downgrade an unparseable value to None
        # with a logged warning rather than raising.
        if value is None:
            return None
        try:
            date.fromisoformat(value)
        except ValueError:
            logger.warning(
                "Gemini returned a non-ISO-8601 extracted_date %r -- treating as not found", value
            )
            return None
        return value


_EXTRACTION_SYSTEM_PROMPT = """\
You are extracting an application deadline from an opportunity's raw text \
(a scholarship, internship, volunteer position, or course listing) for a \
reminder-scheduling feature.

Rules:
- Find the date the application itself is due -- not a program start date, \
an event date, a founding year, or any other date that happens to appear \
in the text. If multiple dates are present, pick the one that's actually \
described as the application/submission deadline.
- Return `extracted_date` in strict ISO-8601 date format (YYYY-MM-DD). If \
the text gives a date without a full day (e.g. just a month and year), or \
doesn't state a deadline at all, return null rather than guessing a \
specific day.
- `confidence_score` (0-100) reflects how confident you are that \
`extracted_date` is correct AND that it's genuinely the application \
deadline specifically -- not just confidence that some date exists \
somewhere in the text. If `extracted_date` is null, confidence_score \
should be low.
- `original_text_reference` must be the exact snippet the date was found \
in, quoted verbatim from the source text -- not a paraphrase. Null if no \
date was found.
- Do not invent or infer a deadline that isn't actually stated or clearly \
implied by explicit wording in the text.
"""


class ExtractorGateway:
    """Async wrapper around Gemini deadline extraction via the
    `google-genai` SDK -- same package/model-substitution rationale as
    the other Gemini-backed services in this codebase (gemini-1.5-flash
    / google-generativeai are both stale; see the README note on this
    from Step 3.4)."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def extract_deadline(self, opportunity_text: str) -> ExtractedDeadline:
        """Extract an application deadline from `opportunity_text` -- a
        single block of text describing the opportunity (see
        extractor.py's router for what's concatenated into this in
        practice)."""
        client = self._client()

        prompt = f"Opportunity text:\n{opportunity_text}"

        response = await client.aio.models.generate_content(
            model=settings.gemini_extractor_model,
            contents=[_EXTRACTION_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ExtractedDeadline,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for deadline extraction")

        return ExtractedDeadline.model_validate_json(response.text)


extractor_gateway = ExtractorGateway()
