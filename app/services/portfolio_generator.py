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


class PortfolioResult(BaseModel):
    """Structured output for `generate_portfolio_html`. Passed as
    `response_schema` with `response_mime_type="application/json"` and
    validated against this shape on our side -- same posture as every
    other Gemini-backed service here."""

    html_code: str = Field(
        description="A complete, single-file HTML document (starting "
        "with <!DOCTYPE html>, ending with </html>) -- valid HTML only, "
        "no Markdown code fences or explanatory text around it."
    )
    suggestions: str = Field(
        description="Short, plain-language text on real-world links the "
        "student should add themselves before publishing -- e.g. "
        "LinkedIn, GitHub, a real email address -- since none of those "
        "were fabricated into the generated page."
    )


_PORTFOLIO_SYSTEM_PROMPT = """\
You are an expert frontend developer building a single-file, responsive, \
modern personal portfolio webpage for a student, based on their profile text.

Output requirements for `html_code` -- these are strict, not stylistic \
suggestions:
- `html_code` must be a COMPLETE, valid HTML document: starting with \
`<!DOCTYPE html>`, followed by a single `<html>` root element containing \
exactly one `<head>` and one `<body>`, and ending with `</html>`. Every tag \
you open must be closed. Do not truncate the document or omit the closing \
tags.
- Include `<meta charset="UTF-8">` and a responsive viewport meta tag \
(`<meta name="viewport" content="width=device-width, initial-scale=1.0">`) \
in `<head>`.
- Load Tailwind CSS via its CDN script tag in `<head>` \
(`<script src="https://cdn.tailwindcss.com"></script>`) -- do not write raw \
custom CSS in a `<style>` block as the primary styling mechanism; use \
Tailwind utility classes for layout and styling instead.
- The single file must be self-contained: no references to external images, \
fonts, or scripts other than the Tailwind CDN script itself.
- Do NOT wrap `html_code` in Markdown code fences (no ```html or ```) and do \
NOT include any explanatory text before or after the HTML -- the field's \
entire value must be parseable as one HTML document, nothing else.
- The page must be responsive: use Tailwind's responsive utility classes \
(e.g. sm:/md:/lg: prefixes) so it reads well on both mobile and desktop \
widths, not just a fixed desktop layout.

Content requirements:
- Include distinct sections, in order: About Me, Education, Skills, and \
Projects/Achievements -- populated from the student's actual profile text \
below.
- Use ONLY information present in the student's profile text. Do not invent \
specific projects, grades, institutions, dates, or achievements that \
weren't actually mentioned -- if a section has little to draw from, keep \
that section brief and honest rather than padding it with fabricated \
content.
- Apply the given accent/theme color (as a Tailwind color name, e.g. \
"blue", "emerald", "rose") to headings, buttons, or section accents \
throughout the page for visual consistency -- don't ignore it or apply it \
inconsistently.
- Do not invent or hardcode fake social links, email addresses, or phone \
numbers -- if the profile text doesn't provide them, either omit a contact \
section's specific links or use clearly generic placeholder text (e.g. \
"[Add your email]") rather than a fabricated-looking real address.

For `suggestions`: briefly tell the student what real-world links they \
should manually add before publishing this page (e.g. their actual \
LinkedIn/GitHub/portfolio links, a real contact email) -- reflecting that \
this generated page cannot know or invent those for them.
"""


def _format_theme_instruction(theme_color: str) -> str:
    return f"Accent/theme color to use throughout (Tailwind color name): {theme_color}"


class PortfolioGateway:
    """Async wrapper around Gemini portfolio-website generation via the
    `google-genai` SDK -- same package/model-substitution rationale as
    the other Gemini-backed services in this codebase (gemini-1.5-flash
    / google-generativeai are both stale; see the README note on this
    from Step 3.4)."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def generate_portfolio_html(
        self, student_profile_text: str, theme_color: str = "blue"
    ) -> PortfolioResult:
        """Generate a single-file HTML portfolio page from
        `student_profile_text`, accented with `theme_color`."""
        client = self._client()

        prompt = (
            f"Student profile text:\n{student_profile_text}\n\n"
            f"{_format_theme_instruction(theme_color)}"
        )

        response = await client.aio.models.generate_content(
            model=settings.gemini_portfolio_model,
            contents=[_PORTFOLIO_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=PortfolioResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for portfolio generation")

        return PortfolioResult.model_validate_json(response.text)


portfolio_gateway = PortfolioGateway()
