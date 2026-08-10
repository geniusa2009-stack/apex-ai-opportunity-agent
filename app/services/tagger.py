import logging

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, Field
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings

logger = logging.getLogger(__name__)

# Same retry posture as grader.py/planner.py/vision.py: `errors.ClientError`
# (429s) and `errors.ServerError` (5xx) both inherit from `errors.APIError`.
_retry_gemini_call = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(genai_errors.APIError),
)


class TagsResult(BaseModel):
    """Structured output for `generate_tags`. Passed as `response_schema`
    with `response_mime_type="application/json"` and validated against
    this shape on our side -- same posture as every other Gemini-backed
    service in this codebase."""

    tags: list[str] = Field(
        default_factory=list,
        description="Standardized, deduplicated tags describing this "
        "opportunity across level of study, field of study, funding "
        "type, and location.",
    )


_TAGGING_SYSTEM_PROMPT = """\
You are a tagging system for a scholarship/internship/volunteer/course \
aggregation platform. Your job is to read an opportunity's text and \
produce a single flat list of standardized, filterable tags.

Cover these categories where the text supports them, but return them all \
as one flat `tags` list (not grouped by category):
- Level of study, e.g. "Undergraduate", "Graduate", "PhD", "High School".
- Field of study, e.g. "Engineering", "Medicine", "Computer Science", \
"Business" -- use the specific field(s) actually mentioned, not a vague \
catch-all.
- Funding type, e.g. "Fully Funded", "Partial Funding", "Unfunded", \
"Stipend Provided".
- Location, e.g. a specific country ("Germany") or region ("Europe") if \
stated.

Rules:
- Only produce a tag when the text actually supports it. If a category \
isn't mentioned or determinable (e.g. no field of study is stated), omit \
tags for that category entirely rather than guessing.
- Use consistent, standardized phrasing (e.g. always "Fully Funded", not \
"fully-funded" or "100% funded") so tags are filterable/comparable across \
many opportunities, not just this one.
- No duplicate or near-duplicate tags in the list.
- Do not invent categories or facts (e.g. a specific GPA cutoff, a specific \
deadline) that aren't part of what a "tag" like this is meant to capture.
"""


class TaggerGateway:
    """Async wrapper around Gemini auto-tagging via the `google-genai`
    SDK -- same package/model-substitution rationale as
    grader.py/planner.py/vision.py (gemini-1.5-flash / google-generativeai
    are both stale; see the README note on this from Step 3.4)."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def generate_tags(self, opportunity_text: str) -> TagsResult:
        """Generate standardized tags from `opportunity_text` -- a single
        block of text describing the opportunity (see tagger.py's router
        for what's concatenated into this in practice)."""
        client = self._client()

        prompt = f"Opportunity text:\n{opportunity_text}"

        response = await client.aio.models.generate_content(
            model=settings.gemini_tagger_model,
            contents=[_TAGGING_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=TagsResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for tag generation")

        return TagsResult.model_validate_json(response.text)


tagger_gateway = TaggerGateway()
