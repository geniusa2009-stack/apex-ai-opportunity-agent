import logging

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, Field
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings

logger = logging.getLogger(__name__)

# `errors.ClientError` and `errors.ServerError` both inherit from
# `errors.APIError` in the current `google-genai` SDK -- catching the base
# class covers rate limits (429s surface as ClientError) and transient
# server-side failures (5xx as ServerError) in one retry policy.
_retry_gemini_call = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(genai_errors.APIError),
)


class ExtractedDocumentSchema(BaseModel):
    """Structured output Gemini is asked to produce from an uploaded
    document image (e.g. an academic transcript). Passed as
    `response_schema` with `response_mime_type="application/json"` in
    `VisionGateway.extract_document` -- the model is instructed to return
    JSON matching this shape, and the response is then validated against
    it on our side rather than trusted blindly."""

    document_type: str = Field(
        description="Best guess at what kind of document this is, e.g. "
        "'academic transcript', 'certificate', 'resume'."
    )
    full_name: str | None = Field(
        default=None, description="Student/candidate name if visible on the document."
    )
    institution: str | None = Field(
        default=None, description="School/university/institution name if visible."
    )
    gpa: float | None = Field(
        default=None,
        description="GPA if visible, using whatever scale the document itself "
        "uses -- do not convert scales.",
    )
    gpa_scale: str | None = Field(
        default=None,
        description="The grading scale in use, e.g. '4.0', '10.0', '100', if determinable.",
    )
    skills: list[str] = Field(
        default_factory=list,
        description="Skills, courses, or competencies explicitly listed on the document.",
    )
    notes: str | None = Field(
        default=None,
        description="Brief notes on anything relevant that doesn't fit the fields "
        "above, or caveats about extraction confidence (e.g. 'GPA partially obscured').",
    )


_EXTRACTION_SYSTEM_PROMPT = """\
You are extracting structured data from an uploaded document image (e.g. an \
academic transcript, certificate, or similar credential) for a scholarship/ \
internship application platform.

Rules:
- Only report what is actually visible and legible in the image. If a field \
isn't present or is illegible, leave it null (or an empty list for skills) \
rather than guessing.
- Do not infer or convert a GPA onto a different scale than what's shown -- \
report the scale exactly as given.
- This extraction feeds into a real application workflow, so accuracy over \
completeness matters: an incomplete but honest extraction is far better than \
a confident-looking fabrication.
"""


class VisionGateway:
    """Async wrapper around Gemini's vision capabilities via the current
    (2024+) unified `google-genai` SDK.

    Deliberately NOT `google-generativeai` (the older package the original
    ask named): Google sunset that SDK on 2025-08-31, so it's no longer
    the correct package to build against. `google-genai` is its official
    replacement, imported as `from google import genai`.

    All calls here go through `client.aio.*`, the SDK's native async
    surface -- unlike `deep_translator` (app/services/translation.py),
    there's no blocking-call-on-the-event-loop problem to route around
    with `asyncio.to_thread`; this genuinely awaits network I/O.
    """

    def _client(self) -> genai.Client:
        # Same "fresh client per call" posture as AIGateway
        # (app/services/ai.py), for the same reason: this method may end
        # up called from contexts with different event loop lifecycles
        # (a persistent FastAPI loop today; potentially a per-task
        # `asyncio.run()` Celery context later), and a client isn't
        # assumed safe to share across them without checking.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def extract_document(self, image_bytes: bytes, mime_type: str) -> ExtractedDocumentSchema:
        client = self._client()
        response = await client.aio.models.generate_content(
            model=settings.gemini_vision_model,
            contents=[
                _EXTRACTION_SYSTEM_PROMPT,
                genai_types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
            ],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ExtractedDocumentSchema,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for document extraction")

        # Validated here via Pydantic rather than trusting an SDK
        # convenience attribute (e.g. a `.parsed` property) to have done
        # it correctly. `response.text` is a stable, documented property;
        # `response_mime_type="application/json"` plus `response_schema`
        # above is what obligates the API to return JSON matching this
        # shape -- this line is what actually confirms it did.
        return ExtractedDocumentSchema.model_validate_json(response.text)


vision_gateway = VisionGateway()
