import logging

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, Field
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings

logger = logging.getLogger(__name__)

# Same retry posture as every other Gemini-backed service in this codebase:
# `errors.ClientError` (429s) and `errors.ServerError` (5xx) both inherit
# from `errors.APIError`.
_retry_gemini_call = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(genai_errors.APIError),
)


class ArtworkNarrative(BaseModel):
    suggested_title: str
    curator_description: str = Field(
        description="A brief, artsy description explaining the depth of this piece."
    )


class PortfolioCuratorResult(BaseModel):
    """Structured output for `curate_portfolio`. Passed as
    `response_schema` with `response_mime_type="application/json"` and
    validated against this shape on our side -- same posture as every
    other Gemini-backed service here."""

    portfolio_overarching_theme: str = Field(
        description="A creative, cohesive concept that ties the student's artwork themes together."
    )
    presentation_and_photography_tips: list[str] = Field(
        description="3 practical tips for a high schooler to photograph/digitize their specific "
        "art medium professionally without expensive gear."
    )
    artwork_narratives: list[ArtworkNarrative] = Field(
        description="One narrative entry per artwork theme provided, each with a suggested_title "
        "and curator_description."
    )
    artist_statement_hook: str = Field(
        description="A strong opening sentence the student can use for their general artist statement."
    )


_PORTFOLIO_CURATOR_SYSTEM_PROMPT = """\
You are an expert art school admissions portfolio reviewer, helping a \
high school student applying to arts, design, or architecture programs \
build a standout, cohesive portfolio. Students often struggle with \
presentation and writing their artist statements more than with the art \
itself.

You will be given the student's art medium and a list of artwork themes.

Your job:
1. Write a `portfolio_overarching_theme` -- a creative, genuinely \
cohesive concept that ties the student's individual artwork themes \
together into a single compelling portfolio narrative, not just a list \
of unrelated pieces.
2. Write exactly 3 `presentation_and_photography_tips` -- practical, \
medium-specific advice for how a high schooler can photograph or \
digitize their SPECIFIC art medium professionally using only what's \
realistically available to them (a smartphone, natural light, free \
editing apps) -- never assume access to expensive professional \
equipment (a dedicated lightbox, a DSLR, a professional scanner).
3. Write `artwork_narratives` -- one entry per theme the student \
provided, each with a `suggested_title` and a `curator_description` \
that captures the conceptual depth of that piece in genuine, \
artsy-but-not-pretentious admissions-portfolio language.
4. Write an `artist_statement_hook` -- one strong, specific opening \
sentence for the student's general artist statement, grounded in their \
actual medium and themes, not a generic quote that could apply to any \
artist.

Rules:
- Ground every recommendation in the student's actual stated medium and \
themes -- avoid generic art-school advice that doesn't reference what \
they specifically told you.
- Keep photography/presentation advice genuinely achievable with \
low-cost or no-cost equipment -- the whole point is making a \
professional-looking portfolio accessible to a student without a \
budget for professional gear.
- Keep tone confident, artistically engaged, and encouraging.
"""


def _format_prompt(art_medium: str, artwork_themes: list[str]) -> str:
    return f"Art medium: {art_medium}\nArtwork themes: {', '.join(artwork_themes)}"


class PortfolioCuratorGateway:
    """Async wrapper around Gemini-based teen art portfolio curation via
    the `google-genai` SDK -- same package/model-substitution rationale
    as every other Gemini-backed service in this codebase
    (gemini-1.5-flash / google-generativeai are both stale; see the
    README note on this from Step 3.4).

    Distinct from `portfolio.py` (Advanced Feature 3, Step 3.17), which
    generates a portfolio WEBSITE from application materials -- this
    service instead curates the CONTENT strategy for an art/design
    admissions portfolio itself. Different filenames/tags avoid any
    naming collision between the two."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def curate_portfolio(
        self, art_medium: str, artwork_themes: list[str]
    ) -> PortfolioCuratorResult:
        """Generate a cohesive art/design admissions portfolio strategy,
        including presentation tips and drafted artwork descriptions,
        based on a student's art medium and artwork themes."""
        client = self._client()

        prompt = _format_prompt(art_medium, artwork_themes)

        response = await client.aio.models.generate_content(
            model=settings.gemini_portfolio_curator_model,
            contents=[_PORTFOLIO_CURATOR_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=PortfolioCuratorResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for portfolio curation")

        return PortfolioCuratorResult.model_validate_json(response.text)


portfolio_curator_gateway = PortfolioCuratorGateway()
