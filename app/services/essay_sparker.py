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


class EssayConcept(BaseModel):
    theme: str
    opening_hook: str = Field(description="A catchy first sentence for this essay concept.")
    narrative_arc: str = Field(description="How the story should progress from beginning to end.")
    connection_to_major: str = Field(
        description="How this story subtly proves the student is a fit for their target major."
    )


class EssaySparkerResult(BaseModel):
    """Structured output for `generate_essay_ideas`. Passed as
    `response_schema` with `response_mime_type="application/json"` and
    validated against this shape on our side -- same posture as every
    other Gemini-backed service here."""

    brainstorming_summary: str = Field(
        description="A brief psychological profile of the student based on their inputs."
    )
    essay_concepts: list[EssayConcept] = Field(
        description="3 distinct essay ideas drawn from the student's own life events, values, and target major."
    )
    writing_advice: list[str] = Field(
        description="2 'Do's and Don'ts' specifically relevant to these essay ideas."
    )


_ESSAY_SPARKER_SYSTEM_PROMPT = """\
You are a master storytelling coach and Ivy League admissions essay \
expert, helping a high school student who feels they "don't have a \
unique story" find compelling personal-statement material (like a \
Common App essay) in their own ordinary life.

You will be given the student's life events (which may seem mundane to \
them), core values, and target undergraduate major.

Your job:
1. Write a `brainstorming_summary` -- a brief, genuinely insightful \
psychological profile of the student based specifically on what they \
told you, identifying real patterns or tensions in their values and \
experiences, not generic personality-test language.
2. Generate exactly 3 `essay_concepts`, each genuinely distinct from \
the others (different events, different angles, or different framings \
of the same events) and each finding real, profound meaning in a \
seemingly ordinary event the student described -- the whole point of \
this feature is proving a "boring" life event can become a compelling \
essay. Each concept needs a `theme`, a genuinely catchy `opening_hook` \
(an actual first sentence, not a description of one), a `narrative_arc` \
describing how the story should progress from beginning to end, and a \
`connection_to_major` explaining how the story subtly (not heavy- \
handedly) suggests fit for their target major.
3. Write exactly 2 `writing_advice` items -- concrete "Do's and Don'ts" \
specific to these particular essay ideas, not generic essay-writing \
advice that could apply to any student.

Rules:
- Ground every essay concept in something the student actually told \
you -- do not invent life events, achievements, or experiences they did \
not mention.
- Avoid cliche personal-statement tropes (e.g. "the big game," \
generic mission-trip epiphanies) unless the student's own input \
genuinely points there -- lean into the specific and the real.
- Keep tone warm, insightful, and encouraging -- many students feel \
anxious about this essay, and the goal is to make them feel like their \
story is genuinely worth telling.
"""


def _format_prompt(life_events: list[str], core_values: list[str], target_major: str) -> str:
    return (
        f"Life events: {', '.join(life_events)}\n"
        f"Core values: {', '.join(core_values)}\n"
        f"Target major: {target_major}"
    )


class EssaySparkerGateway:
    """Async wrapper around Gemini-based Common App essay idea sparking
    via the `google-genai` SDK -- same package/model-substitution
    rationale as every other Gemini-backed service in this codebase
    (gemini-1.5-flash / google-generativeai are both stale; see the
    README note on this from Step 3.4)."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def generate_essay_ideas(
        self, life_events: list[str], core_values: list[str], target_major: str
    ) -> EssaySparkerResult:
        """Turn a student's ordinary life events, core values, and
        target major into compelling personal-statement narrative arcs
        and hooks."""
        client = self._client()

        prompt = _format_prompt(life_events, core_values, target_major)

        response = await client.aio.models.generate_content(
            model=settings.gemini_essay_sparker_model,
            contents=[_ESSAY_SPARKER_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=EssaySparkerResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for essay idea sparking")

        return EssaySparkerResult.model_validate_json(response.text)


essay_sparker_gateway = EssaySparkerGateway()
