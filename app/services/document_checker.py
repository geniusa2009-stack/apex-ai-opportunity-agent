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


class DocumentCheckResult(BaseModel):
    """Structured output for `analyze_document_image`. Passed as
    `response_schema` with `response_mime_type="application/json"` and
    validated against this shape on our side -- same posture as
    `ExtractedDocumentSchema` in vision.py (Step 3.4), the other
    vision-based feature in this codebase."""

    is_acceptable: bool = Field(
        description="Whether this document would likely pass an "
        "admissions officer's review as-is, with no critical issues "
        "found."
    )
    issues_found: list[str] = Field(
        default_factory=list,
        description="Concrete, specific issues or warnings actually "
        "observed in the image -- e.g. 'The passport expires in 2 "
        "months', 'No official university stamp detected', 'Bottom "
        "third of the page is too blurry to read'. Empty list if the "
        "document looks acceptable.",
    )
    confidence_score: int = Field(
        ge=0,
        le=100,
        description="Confidence (0-100) in this specific "
        "is_acceptable/issues_found judgment -- lower when the image "
        "quality itself makes the document hard to assess either way.",
    )


_DOCUMENT_CHECK_SYSTEM_PROMPT = """\
You are a strict admissions officer reviewing an uploaded document image \
(e.g. a passport, an academic transcript, a translated certificate) before \
it's submitted as part of a real application.

Check specifically for:
- Expiration dates: if a date of expiry is visible and it's already passed, \
or is very close to passing, flag it explicitly with the actual date/ \
timeframe you see.
- Official stamps, seals, or signatures: if this looks like the kind of \
document that should have an official stamp/seal/signature (e.g. a \
notarized translation, an official transcript) and none is visible, flag \
that specifically.
- Legibility: if any part of the document is too blurry, cropped, dark, or \
obscured to read clearly, flag which part and what's illegible.
- Anything else concretely wrong that would cause a real reviewer to reject \
or query this document (e.g. a mismatched name, a page clearly cut off).

Rules:
- Only report issues you can actually observe in the image. Do not invent \
problems (e.g. don't claim a stamp is missing if one is visible, or that a \
date is expired without actually reading a date) to seem thorough -- a \
false rejection is as harmful to the student as missing a real problem.
- If the document looks genuinely fine, set `is_acceptable` to true and \
return an empty `issues_found` list -- do not pad it with minor stylistic \
nitpicks that wouldn't actually cause a rejection.
- `is_acceptable` should be false if ANY critical issue is found (expired \
date, missing required stamp/signature, illegible content) -- a document \
with only truly minor cosmetic issues can still be `is_acceptable: true` \
with those minor issues still listed.
- Lower `confidence_score` when the image quality itself (not the document's \
content) makes it hard to judge -- e.g. a low-resolution photo where you \
genuinely can't tell if a stamp is present or not.
"""


class DocumentCheckerGateway:
    """Async wrapper around Gemini vision-based document authenticity
    checking via the `google-genai` SDK -- same package/model-
    substitution rationale as `vision.py` (Step 3.4) and every other
    Gemini-backed service in this codebase (gemini-1.5-flash /
    google-generativeai are both stale; see the README note on this
    from Step 3.4)."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def analyze_document_image(
        self, image_bytes: bytes, mime_type: str = "image/jpeg"
    ) -> DocumentCheckResult:
        """Analyze `image_bytes` (a document photo/scan) for common
        acceptance issues: expiration dates, missing official stamps/
        signatures, and legibility problems."""
        client = self._client()

        response = await client.aio.models.generate_content(
            model=settings.gemini_document_checker_model,
            contents=[
                _DOCUMENT_CHECK_SYSTEM_PROMPT,
                genai_types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            ],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=DocumentCheckResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for document authenticity check")

        return DocumentCheckResult.model_validate_json(response.text)


document_checker_gateway = DocumentCheckerGateway()
