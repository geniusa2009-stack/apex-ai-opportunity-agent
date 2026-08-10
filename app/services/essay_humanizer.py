import logging

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, Field
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings

logger = logging.getLogger(__name__)

# Same retry posture as every other Gemini-backed service in this codebase.
_retry_gemini_call = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(genai_errors.APIError),
)


class EssayHumanizationError(Exception):
    """Raised when the Gemini analyze-and-humanize call fails."""


class FlaggedSentence(BaseModel):
    sentence: str = Field(
        description="The exact sentence, quoted verbatim from the original "
        "essay, that reads as clinical/robotic/AI-generated."
    )
    reason_why_robotic: str = Field(
        description="A specific, concrete explanation of what makes this "
        "sentence sound AI-generated (e.g. generic buzzword vocabulary, "
        "unnaturally uniform rhythm, hedge-free overconfidence, cliché "
        "structure) -- not a vague 'sounds robotic'."
    )


class EssayHumanizationResult(BaseModel):
    """Structured output for `analyze_and_humanize`, per the ask's exact
    field list. Also the shape persisted as this feature's history
    artifact payload (see `app/api/v1/endpoints/essay_humanizer.py`)."""

    estimated_ai_score: int = Field(
        ge=0,
        le=100,
        description="Estimated likelihood (0-100) that an AI-detection "
        "tool like GPTZero or Turnitin would flag the ORIGINAL essay (not "
        "the humanized rewrite) as AI-generated. 0 = reads as "
        "unmistakably human, 100 = reads as unmistakably AI-generated.",
    )
    flagged_sentences: list[FlaggedSentence] = Field(
        default_factory=list,
        description="Specific sentences from the ORIGINAL essay that read "
        "as clinical/robotic/AI-generated. Empty if none genuinely stand out.",
    )
    humanized_essay: str = Field(
        description="The fully rewritten essay: the same story, facts, and "
        "claims as the original, but in an authentic, natural, distinctly "
        "human voice in the requested target tone."
    )
    # "a list of 3 specific changes" in the ask -- a bare count, not a
    # hedged "roughly"/"a few"/range like the 3-5 milestone count in the
    # previous Ecosystem step -- read as a genuine fixed requirement and
    # hard-constrained accordingly.
    humanization_techniques_used: list[str] = Field(
        min_length=3,
        max_length=3,
        description="Exactly 3 specific, concrete changes actually made "
        "to THIS essay -- e.g. 'Replaced \"leverage\" with \"used\" in "
        "paragraph 2' or 'Broke the uniform ~20-word sentences in the "
        "opening into a 6-word sentence followed by a 31-word one' -- not "
        "generic boilerplate like 'Improved flow.'",
    )


_HUMANIZER_SYSTEM_PROMPT = """\
You are an elite Ivy League admissions essay reviewer and a master of prose \
stylistics, with deep expertise in what distinguishes authentic human writing \
from AI-generated text -- including what modern AI-detection tools (like \
GPTZero and Turnitin) actually key off of.

You will be given a student's motivation letter / personal statement essay \
and a target tone for the rewrite.

Your job has two parts.

PART 1 -- ANALYZE:
- Estimate `estimated_ai_score` (0-100): how likely an AI-detection tool \
would flag the ORIGINAL essay as AI-generated. Be genuinely calibrated -- \
not reflexively high or low.
- Identify `flagged_sentences`: specific sentences (quoted verbatim from the \
original) that read as clinical, robotic, or generically "AI-sounding," each \
with a concrete, specific reason why.

PART 2 -- HUMANIZE:
Rewrite the essay into `humanized_essay`, in the requested target tone, so it \
reads as authentically, distinctly human -- indistinguishable from the work \
of a highly talented human writer. Specifically:
- ELIMINATE generic AI vocabulary and clichés -- words and phrases like \
"delve," "foster," "leverage," "testament," "navigate," "realm," "tapestry," \
"boundless," "embark on a journey," "underscore," "landscape," "robust," \
"multifaceted," "intricate," "paramount," "pivotal," "bespoke," "elevate," \
"unveil," "harness," and "unwavering" -- and any other words that have \
become well-known AI-writing tells. Replace them with plain, specific, \
natural language a real person would actually say.
- INTRODUCE genuine emotional depth and specificity -- concrete sensory \
detail, real interiority, honest imperfection -- grounded ONLY in what the \
original essay actually describes (see the critical rule below).
- VARY sentence length and rhythm dramatically -- mix short, punchy \
sentences with longer, more complex ones. Uniform, evenly-paced sentence \
structure is itself one of the strongest AI-detection signals; a natural \
human essay has real rhythmic variation.
- Break up predictable, formulaic paragraph structures (e.g. "Firstly... \
Secondly... In conclusion...") into something that reads as genuinely \
composed, not templated.

CRITICAL RULE -- never invent: the humanized essay must tell the exact same \
story, with the exact same facts, experiences, and claims, as the original. \
You may restyle, restructure, rephrase, and add authentic voice and \
emotional framing -- but you must NEVER invent a new fact, experience, \
achievement, or detail that wasn't in the original essay. This is a real \
document a real student may submit to a real institution; fabricating \
content on their behalf would be a serious problem for them, not a \
stylistic improvement.

Finally, in `humanization_techniques_used`, list EXACTLY 3 specific changes \
you actually made to THIS essay (not generic advice) -- concrete enough that \
someone could verify each one by comparing the original and rewritten text.
"""


def _client() -> genai.Client:
    # Same "fresh client per call" posture as every other Gemini-backed
    # service in this codebase -- see VisionGateway._client
    # (app/services/vision.py) for the full event-loop-lifetime reasoning.
    return genai.Client(api_key=settings.gemini_api_key)


@_retry_gemini_call
async def analyze_and_humanize(essay_text: str, target_tone: str) -> EssayHumanizationResult:
    prompt = f"Target tone: {target_tone}\n\nOriginal essay:\n{essay_text}"

    try:
        response = await _client().aio.models.generate_content(
            model=settings.gemini_essay_humanizer_model,
            contents=[_HUMANIZER_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=EssayHumanizationResult,
            ),
        )
    except genai_errors.APIError:
        raise
    except Exception as exc:
        raise EssayHumanizationError(str(exc)) from exc

    if not response.text:
        raise EssayHumanizationError("Gemini returned no content for essay humanization")

    return EssayHumanizationResult.model_validate_json(response.text)
