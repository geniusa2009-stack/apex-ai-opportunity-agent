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


class AutofillSchema(BaseModel):
    """Structured output for `generate_autofill_schema`. Passed as
    `response_schema` with `response_mime_type="application/json"` and
    validated against this shape on our side -- same posture as every
    other Gemini-backed service here.

    Every field is a plain string (or null), even `gpa` and
    `graduation_year` which are numeric on the `User` model -- HTML form
    inputs and browser autofill/extension APIs are inherently text-based,
    so representing every value as a string keeps the "map this key to
    that form field" contract simple and uniform for whoever consumes
    this on the frontend, rather than mixing types the caller has to
    branch on.
    """

    first_name: str | None = Field(default=None, description="Split from the user's full name.")
    last_name: str | None = Field(default=None, description="Split from the user's full name.")
    email: str | None = None
    phone: str | None = None
    degree_level: str | None = Field(
        default=None,
        description="Standardized wording, e.g. 'Undergraduate', 'Master's', 'PhD' -- "
        "not whatever inconsistent phrasing the user originally typed.",
    )
    major: str | None = Field(
        default=None, description="Standardized/cleaned field of study name."
    )
    gpa: str | None = Field(default=None, description="Formatted as a plain string, e.g. '3.8'.")
    graduation_year: str | None = Field(default=None, description="Formatted as a 4-digit string.")
    skills_comma_separated: str | None = Field(
        default=None, description="Comma-separated list of skills, e.g. 'Python, SQL, Public Speaking'."
    )


_AUTOFILL_SYSTEM_PROMPT = """\
You are structuring a student's profile information into a flat, standardized \
JSON object designed to feed a browser auto-fill extension or programmatic \
form submission on external university/application portals.

You will be given the student's raw profile fields as text (some of which may \
already be reasonably clean, some of which may be inconsistently phrased -- \
e.g. a major typed as "cs" or "comp. sci" instead of "Computer Science").

Your job:
1. Split the full name into `first_name` and `last_name`. If there's a middle \
name, include it with `first_name` (e.g. "Mary Jane Smith" -> first_name \
"Mary Jane", last_name "Smith") rather than dropping it.
2. Standardize `degree_level` and `major` into clean, conventional phrasing \
suitable for a dropdown or text field on a real application portal (e.g. \
"undergrad" -> "Undergraduate"; "cs" -> "Computer Science") -- but do not \
change what they actually said, just clean up the phrasing.
3. Format `gpa` and `graduation_year` as plain strings.
4. Join skills into `skills_comma_separated`, a single comma-separated string.

Rules:
- If a field wasn't provided in the input at all, leave it null -- do not \
invent a value, a plausible-sounding email, phone number, or any other detail \
that wasn't actually given.
- Do not change the actual substance of what was provided (e.g. don't \
"correct" a GPA number or invent a major that wasn't stated) -- only clean up \
phrasing/formatting/structure.
"""


def format_profile_text(profile_fields: dict) -> str:
    """Public (not underscore-prefixed) because the router builds
    `profile_fields` from the `User` model and needs to call this to
    produce the text `generate_autofill_schema` expects -- this is a
    cross-module helper, not a private implementation detail."""
    lines = []
    for label, key in (
        ("Full name", "full_name"),
        ("Email", "email"),
        ("Phone", "phone"),
        ("Degree level", "degree_level"),
        ("Major", "major"),
        ("GPA", "gpa"),
        ("Graduation year", "graduation_year"),
        ("Skills", "skills"),
    ):
        value = profile_fields.get(key)
        if value:
            lines.append(f"{label}: {value}")
    return "\n".join(lines)


class AutofillAgentGateway:
    """Async wrapper around Gemini-based auto-fill schema generation via
    the `google-genai` SDK -- same package/model-substitution rationale
    as every other Gemini-backed service in this codebase
    (gemini-1.5-flash / google-generativeai are both stale; see the
    README note on this from Step 3.4)."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def generate_autofill_schema(self, student_profile_text: str) -> AutofillSchema:
        """Parse `student_profile_text` (free-text profile fields, see
        `_format_profile_text` for the shape the router builds this in)
        into a standardized, flat autofill schema."""
        client = self._client()

        prompt = f"Student profile:\n{student_profile_text}"

        response = await client.aio.models.generate_content(
            model=settings.gemini_autofill_model,
            contents=[_AUTOFILL_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=AutofillSchema,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for autofill schema generation")

        return AutofillSchema.model_validate_json(response.text)


autofill_agent_gateway = AutofillAgentGateway()
