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


class SafetyFilterResult(BaseModel):
    """Structured output for `analyze_safety_and_funding`. Passed as
    `response_schema` with `response_mime_type="application/json"` and
    validated against this shape on our side -- same posture as every
    other Gemini-backed service here.

    `final_verdict` stays a plain string, not a `Literal`, despite the
    ask giving two example values ("Highly Recommended", "Proceed with
    Caution") -- both are explicitly "e.g."-prefixed in the ask, so this
    follows the established "e.g." vs. bracket-only distinction (see
    extracurricular_architect.py's docstring) rather than the
    exact-enum treatment used for genuinely bracket-specified fields.
    A near-miss verdict phrasing (e.g. "Recommended with Reservations")
    shouldn't hard-fail a request that otherwise did its job correctly.
    """

    funding_status_analysis: str = Field(
        description="Whether this scholarship typically covers absolutely everything, or only partially."
    )
    hidden_costs_warning: list[str] = Field(
        description="2-3 things usually NOT covered that parents should prepare for, "
        "e.g. visa fees, winter clothing."
    )
    city_safety_evaluation: str = Field(
        description="An assessment of the city/university regarding crime rates, healthcare, "
        "and friendliness towards international minors."
    )
    final_verdict: str = Field(
        description='E.g. "Highly Recommended", "Proceed with Caution".'
    )


_SAFETY_FILTER_SYSTEM_PROMPT = """\
You are a strict International Student Safety & Financial Advisor, \
evaluating whether a specific scholarship and destination are genuinely \
suitable for a HIGH SCHOOL student (a minor) who typically cannot work \
part-time abroad due to age restrictions and therefore needs a truly \
100% fully-funded opportunity, and whose parents' top priority is \
safety.

You will be given a scholarship name and a target city or university.

Your job:
1. Write `funding_status_analysis` -- a clear, honest explanation of \
whether this specific scholarship typically covers genuinely everything \
(tuition, housing, food, flights, insurance) or is actually only \
partial funding that a family would still need to supplement -- be \
skeptical and precise here, since "fully-funded" is sometimes used \
loosely by programs that don't actually cover everything.
2. List 2-3 `hidden_costs_warning` items -- concrete things usually NOT \
covered even by generous scholarships (e.g. visa application fees, \
winter clothing, local transport, personal spending money, return \
flights home for holidays).
3. Write a `city_safety_evaluation` -- an honest assessment of the \
specific city/university regarding crime rates, healthcare access, and \
how welcoming/prepared it is for international minors specifically. \
Speak in general, honest, well-known terms about the destination \
(e.g. "generally considered a safe university town with strong \
international student support" or "a large city that requires normal \
urban safety precautions") rather than inventing specific crime \
statistics, dates, or incidents you don't actually know are accurate -- \
and explicitly note in this field that families should verify current \
safety conditions against official sources (e.g. their home country's \
government travel advisories, the university's international student \
office) before making a final decision, since safety conditions can \
change over time.
4. Write a `final_verdict` -- a short, honest summary judgment (e.g. \
"Highly Recommended", "Proceed with Caution", "Not Recommended for a \
Minor Without Additional Support") based on the actual funding and \
safety analysis above, not a reflexively positive verdict.

Rules:
- Never state a specific crime statistic, safety incident, or dollar \
figure as fact unless it is genuinely well-known and accurate -- when \
uncertain, speak in general, honest terms and explicitly recommend the \
family verify against current official sources. This assessment could \
genuinely influence whether a family sends their minor child abroad --  \
treat that responsibility seriously and never inflate confidence you \
don't actually have.
- Be willing to give a genuinely cautious or negative verdict when \
warranted -- do not default to reassuring language if the honest \
analysis doesn't support it.
- Keep tone direct, professional, and genuinely protective of the \
student's wellbeing.
"""


def _format_prompt(scholarship_name: str, city_or_university: str) -> str:
    return f"Scholarship name: {scholarship_name}\nCity or university: {city_or_university}"


class SafetyFilterGateway:
    """Async wrapper around Gemini-based fully-funded/safety analysis
    via the `google-genai` SDK -- same package/model-substitution
    rationale as every other Gemini-backed service in this codebase
    (gemini-1.5-flash / google-generativeai are both stale; see the
    README note on this from Step 3.4)."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def analyze_safety_and_funding(
        self, scholarship_name: str, city_or_university: str
    ) -> SafetyFilterResult:
        """Analyze a scholarship's true funding comprehensiveness and a
        destination's safety profile specifically for a minor high
        school student."""
        client = self._client()

        prompt = _format_prompt(scholarship_name, city_or_university)

        response = await client.aio.models.generate_content(
            model=settings.gemini_safety_filter_model,
            contents=[_SAFETY_FILTER_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=SafetyFilterResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for safety and funding analysis")

        return SafetyFilterResult.model_validate_json(response.text)


safety_filter_gateway = SafetyFilterGateway()
