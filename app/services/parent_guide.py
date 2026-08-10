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


class ParentGuideResult(BaseModel):
    """Structured output for `generate_parent_guide`. Passed as
    `response_schema` with `response_mime_type="application/json"` and
    validated against this shape on our side -- same posture as every
    other Gemini-backed service here."""

    safety_and_supervision_reassurance: str = Field(
        description="A comforting paragraph on typical under-18 supervision, safety, and cultural support "
        "in the target country."
    )
    financial_aid_explanation: str = Field(
        description="A simplified breakdown of what the financial need type entails and how much it typically covers."
    )
    actionable_parent_steps: list[str] = Field(
        description="3 specific tasks the parents need to do, like preparing tax documents or signing consent forms."
    )
    conversation_starters: list[str] = Field(
        description="2 suggested questions parents should discuss with their child to align expectations."
    )


_PARENT_GUIDE_SYSTEM_PROMPT = """\
You are a highly reassuring, culturally sensitive international \
education counselor, speaking directly to concerned parents of a minor \
(under 18) student who has been offered or is applying to a scholarship \
abroad. Parental buy-in is often the biggest hurdle for students under \
18 -- parents worry about safety, supervision, and complex financial aid \
paperwork (like the CSS Profile).

You will be given the scholarship name, the target country, and the \
type of financial need involved.

Your job:
1. Write `safety_and_supervision_reassurance` -- a genuinely comforting, \
honest paragraph explaining typical under-18 supervision structures, \
safety norms, and cultural/community support systems in the target \
country (e.g. host family vetting, on-campus residential supervision, \
international student support offices) -- speak in general, accurate \
terms about how these programs typically operate, not fabricated \
specific statistics or invented claims about a specific institution's \
exact policies that you don't actually know.
2. Write a `financial_aid_explanation` -- a simplified, jargon-free \
breakdown of what the stated financial need type genuinely entails \
(e.g. what "full need-based aid" typically covers vs. what a family \
might still need to budget for) and roughly how much it typically \
covers, in a way a parent unfamiliar with international financial aid \
terminology can actually follow.
3. Write exactly 3 `actionable_parent_steps` -- specific, concrete tasks \
parents actually need to do (e.g. preparing tax documents, signing \
consent/guardianship forms, arranging a passport/visa appointment).
4. Write exactly 2 `conversation_starters` -- thoughtful questions the \
parents should discuss with their child to align expectations before \
committing (e.g. around communication frequency, handling homesickness, \
financial boundaries).

Rules:
- Never state a specific safety statistic, specific institutional \
policy, or specific financial aid dollar figure as fact unless it is \
genuinely well-known and accurate -- when you don't know a precise \
detail, speak in honest, general, reassuring terms and note that the \
family should confirm specifics directly with the scholarship program \
or host institution. This is safety and financial information given to \
parents of a minor -- honesty and appropriate caution matter more here \
than in almost any other feature in this platform.
- Keep tone warm, respectful, and genuinely reassuring without being \
dismissive of real parental concerns.
"""


def _format_prompt(scholarship_name: str, target_country: str, financial_need_type: str) -> str:
    return (
        f"Scholarship name: {scholarship_name}\n"
        f"Target country: {target_country}\n"
        f"Financial need type: {financial_need_type}"
    )


class ParentGuideGateway:
    """Async wrapper around Gemini-based parent consent & financial
    guide generation via the `google-genai` SDK -- same package/model-
    substitution rationale as every other Gemini-backed service in this
    codebase (gemini-1.5-flash / google-generativeai are both stale; see
    the README note on this from Step 3.4)."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def generate_parent_guide(
        self, scholarship_name: str, target_country: str, financial_need_type: str
    ) -> ParentGuideResult:
        """Generate a reassuring, easy-to-understand guide for parents
        covering safety/supervision and financial aid, tailored to a
        specific scholarship, target country, and financial need type."""
        client = self._client()

        prompt = _format_prompt(scholarship_name, target_country, financial_need_type)

        response = await client.aio.models.generate_content(
            model=settings.gemini_parent_guide_model,
            contents=[_PARENT_GUIDE_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ParentGuideResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for parent guide generation")

        return ParentGuideResult.model_validate_json(response.text)


parent_guide_gateway = ParentGuideGateway()
