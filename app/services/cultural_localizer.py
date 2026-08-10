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


class CulturalLocalizationResult(BaseModel):
    """Structured output for `localize_sop`. Passed as `response_schema`
    with `response_mime_type="application/json"` and validated against
    this shape on our side -- same posture as every other Gemini-backed
    service here.

    `tone_score` is "higher is better" (how well the ORIGINAL draft
    already matched the target culture, before rewriting) -- same
    direction as every other score field in this project except
    devils_advocate.py's deliberately inverted `vulnerability_score`.
    """

    localized_text: str = Field(
        description="The fully rewritten, polished SOP/motivation letter, "
        "adapted for the target country's academic and cultural "
        "expectations."
    )
    cultural_adjustments: list[str] = Field(
        default_factory=list,
        description="Specific changes made and why, tied to the target "
        "country's actual cultural/academic mindset -- e.g. 'Reframed the "
        "closing paragraph to emphasize individual leadership, which US "
        "admissions committees weigh heavily, rather than group "
        "achievement.'",
    )
    tone_score: int = Field(
        ge=1,
        le=10,
        description="1-10: how well the ORIGINAL draft (before rewriting) "
        "already matched the target country's cultural/academic "
        "expectations. Higher is better -- matches this project's normal "
        "score direction.",
    )


_CULTURAL_LOCALIZER_SYSTEM_PROMPT = """\
You are an expert international education consultant who helps students \
adapt their Statement of Purpose (SOP) or Motivation Letter to match the \
academic and cultural expectations of a specific target country's \
admissions committees.

Different academic cultures value very different things in an SOP. Use the \
following as detailed reference points, and apply the same kind of \
cultural-fit reasoning to any other country you're given, drawing on your \
own general knowledge of that country's academic norms:

- USA: Emphasize individual leadership, standout personal achievements, \
initiative, and self-confidence. A strong US-style SOP reads as "here is \
what makes me exceptional and how I've already led/driven outcomes."
- Europe / Germany: Emphasize structural and academic logic, realism, \
precision, and collaboration. A strong European/German-style SOP reads as \
methodical and grounded -- clear reasoning for the choice of program, \
realistic goals, and a collaborative (not purely self-promotional) framing.
- Japan / Asia (broadly -- adapt further for the specific country given): \
Emphasize cultural respect, genuine alignment with the institution's own \
values and mission, discipline, and humility. A strong SOP in this style \
avoids overt self-promotion in favor of demonstrating fit, dedication, and \
respect for the institution and its community.

Your job, given the student's original SOP/motivation letter text and a \
target country:

1. Rate `tone_score` (1-10): how well the ORIGINAL draft, as given, already \
matches the target country's cultural/academic expectations described \
above -- honestly, not inflated.
2. Rewrite the SOP so its tone, structure, and framing genuinely fit the \
target country's expectations, producing `localized_text`.
3. List concrete `cultural_adjustments` explaining what was changed and \
specifically why, tied to the target country's actual cultural/academic \
mindset -- not generic writing advice that could apply anywhere.

Rules:
- Localization means changing TONE, STRUCTURE, EMPHASIS, and FRAMING -- it \
does NOT mean inventing new achievements, credentials, experiences, or \
facts the student didn't actually mention in their original text. Every \
concrete claim in `localized_text` must trace back to something the \
original SOP actually said.
- Preserve the student's real underlying story and goals -- you are \
reframing how it's told, not replacing it with a different life story.
- `localized_text` should be a complete, ready-to-use rewritten SOP, not a \
list of suggestions.
- `cultural_adjustments` must be specific to actual changes you made in \
this rewrite, not a generic list of "tips for writing SOPs for Country X."
"""


def _format_prompt(sop_text: str, target_country: str) -> str:
    return (
        f"Target country: {target_country}\n\n"
        f"Original SOP/motivation letter:\n{sop_text}"
    )


class CulturalLocalizerGateway:
    """Async wrapper around Gemini-based SOP cultural localization via the
    `google-genai` SDK -- same package/model-substitution rationale as
    every other Gemini-backed service in this codebase (gemini-1.5-flash
    / google-generativeai are both stale; see the README note on this
    from Step 3.4)."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def localize_sop(self, sop_text: str, target_country: str) -> CulturalLocalizationResult:
        """Rewrite `sop_text` for `target_country`'s academic/cultural
        expectations, with a before-rewrite `tone_score` and a list of
        concrete `cultural_adjustments`."""
        client = self._client()

        prompt = _format_prompt(sop_text, target_country)

        response = await client.aio.models.generate_content(
            model=settings.gemini_cultural_localizer_model,
            contents=[_CULTURAL_LOCALIZER_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=CulturalLocalizationResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for SOP cultural localization")

        return CulturalLocalizationResult.model_validate_json(response.text)


cultural_localizer_gateway = CulturalLocalizerGateway()
