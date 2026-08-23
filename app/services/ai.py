import json
import logging

import anthropic
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from openai import APIConnectionError as OpenAIAPIConnectionError
from openai import APITimeoutError as OpenAIAPITimeoutError
from openai import RateLimitError as OpenAIRateLimitError
from pydantic import BaseModel, Field
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings

logger = logging.getLogger(__name__)

# Must match the `Vector(...)` dimension on Opportunity.embedding
# (app/models/opportunity.py) -- both are sized for text-embedding-3-small.
EMBEDDING_DIM = 1536

# Two providers, two retry policies -- same shape (3 attempts, exponential
# backoff), but each keyed to that provider's own transient-error classes.
# OpenAI and Anthropic's SDKs don't share an exception hierarchy, so one
# shared decorator can't catch both.
_retry_openai_call = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(
        (OpenAIAPIConnectionError, OpenAIAPITimeoutError, OpenAIRateLimitError)
    ),
)

_retry_anthropic_call = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(
        (anthropic.APIConnectionError, anthropic.APITimeoutError, anthropic.RateLimitError)
    ),
)


class VerifiedOpportunitySchema(BaseModel):
    """Structured output the verification call is forced to produce.

    Passed directly as `response_format` to OpenAI's structured outputs
    (`beta.chat.completions.parse`) -- the API guarantees the response
    matches this shape, so there's no hand-rolled JSON parsing here.
    """

    title: str = Field(description="Cleaned, human-readable title of the opportunity.")
    deadline: str | None = Field(
        default=None,
        description=(
            "Application deadline in ISO 8601 format (YYYY-MM-DD or full "
            "datetime), or null if no deadline is stated or it is rolling/ongoing."
        ),
    )
    eligibility_criteria: str | None = Field(
        default=None, description="Cleaned summary of who is eligible to apply."
    )
    funding_details: str | None = Field(
        default=None, description="Cleaned summary of funding/compensation, or null if none."
    )
    is_legitimate: bool = Field(
        description=(
            "Whether this looks like a real, currently valid opportunity -- "
            "not expired, not a scam/spam listing, not a placeholder/test page."
        )
    )
    confidence_score: float = Field(
        ge=0.0, le=1.0, description="Confidence (0-1) in the is_legitimate judgment."
    )

    # --- Backfill Real Eligibility Data step additions ---
    # Deliberately plain `str`/`list[str]` here, not `Gender`/`AcademicLevel`
    # Pydantic enum types -- same established pattern `AIExtractedOpportunityFields.
    # opportunity_type` already uses (a free-text field, defensively coerced
    # AFTER the structured call succeeds via `_coerce_opportunity_type`), so
    # a value Gemini returns that doesn't cleanly match this codebase's real
    # enum vocabulary degrades to a logged warning + `None`/dropped entry
    # (see tasks.py's `_coerce_gender`/`_coerce_academic_levels`) instead of
    # a hard Pydantic validation failure that would throw away the entire
    # otherwise-good verification response over one bad sub-field.
    #
    # ALL FOUR fields share one non-negotiable rule, repeated in the prompt
    # below: null/empty is the correct, default answer whenever the source
    # text doesn't explicitly state (or, for academic level only, clearly
    # name a real education stage for) a restriction. Guessing a restriction
    # the source never mentioned is a worse outcome than leaving a real
    # opportunity looking unrestricted -- it would wrongly hide that
    # opportunity from students who were, in fact, eligible.
    min_age: int | None = Field(
        default=None,
        description=(
            "Minimum age to apply, ONLY if the source text states an explicit "
            "number (e.g. 'ages 16-19', 'must be 18 or older' -> 18) or an "
            "explicit adult-only cutoff. Null if no numeric age restriction is stated."
        ),
    )
    max_age: int | None = Field(
        default=None,
        description=(
            "Maximum age to apply, ONLY if the source text states an explicit "
            "number (e.g. 'ages 16-19' -> 19, 'under 30' -> 29). Null if no "
            "numeric upper age bound is stated."
        ),
    )
    target_gender: str | None = Field(
        default=None,
        description=(
            "'male' or 'female', ONLY if the source text EXPLICITLY restricts "
            "eligibility to one gender (e.g. 'for women only', 'girls "
            "scholarship', 'young men program'). Null if the source is silent "
            "on gender, says 'open to all genders', or is in any way ambiguous. "
            "Never infer this from an opportunity's subject matter or a "
            "source/channel's general audience -- only from an explicit "
            "eligibility statement in THIS listing's own text."
        ),
    )
    target_academic_levels: list[str] | None = Field(
        default=None,
        description=(
            "The specific academic-level values (from this fixed vocabulary: "
            "prep_1, prep_2, prep_3, secondary_1, secondary_2, secondary_3, "
            "university_year_1, university_year_2, university_year_3, "
            "university_year_4, university_year_5, university_year_6, "
            "masters, phd) that this listing explicitly restricts eligibility "
            "to. Only include a level when the source text names a real "
            "education stage this opportunity is FOR (e.g. 'open to "
            "secondary school students' -> "
            "[secondary_1, secondary_2, secondary_3]; 'undergraduate "
            "scholarship' -> every university_year_N value; 'PhD candidates "
            "only' -> [phd]). Null or an empty list if the source states no "
            "academic-level restriction at all, or only a vague phrase like "
            "'students' or 'young people' with no identifiable stage."
        ),
    )


_VERIFICATION_SYSTEM_PROMPT = """\
You are a verification analyst for Apex, a platform aggregating scholarships, \
internships, volunteer positions, and courses. You will be given the raw, \
messy data scraped from a listing page. Your job:

1. Extract a clean title, deadline (ISO 8601 or null), eligibility criteria, \
and funding/compensation details from the raw data.
2. Judge whether this looks like a legitimate, currently valid opportunity -- \
flag scams, expired listings, placeholder/test content, and pages that aren't \
actually opportunities as not legitimate.
3. Give a confidence score (0-1) reflecting how sure you are about the \
legitimacy judgment itself, not how good the opportunity is.
4. Extract structured eligibility RESTRICTIONS, if and only if this specific \
listing's own text explicitly states one:
   - min_age / max_age: only when the text gives an actual number (an explicit \
age range, or an explicit adult/minor cutoff like "18 or older"). Do not \
convert a vague phrase into a guessed number.
   - target_gender: only when the text explicitly restricts eligibility to one \
gender ("women only", "for young men", "girls' scholarship" -> 'female'/'male'). \
A scholarship's SUBJECT (e.g. "Women in STEM Scholarship" as a program NAME) \
does not by itself prove a gender restriction unless the eligibility text \
also restricts WHO may apply -- when in doubt, leave this null.
   - target_academic_levels: only when the text names a real education stage \
this opportunity is restricted to (secondary school, a specific university \
year, undergraduates broadly, master's, PhD). Map it onto the fixed \
academic_level vocabulary described in that field. A vague phrase like \
"students" or "young people" is NOT a stage and must NOT be mapped to any level.

THE SINGLE MOST IMPORTANT RULE FOR STEP 4: leaving a field null/empty because \
the source didn't clearly state a restriction is always the CORRECT, EXPECTED \
answer, not a failure -- most real listings state no restriction at all on \
some or all of these dimensions, and inventing one that isn't in the source \
text would wrongly hide this opportunity from eligible students later. Never \
infer a restriction from an opportunity's general subject, target audience \
guesswork, or the channel/source it came from -- only from this listing's own \
explicit eligibility language.

If the raw data is too sparse to make a confident judgment, set is_legitimate \
to false and give a low confidence_score rather than guessing.
"""


def _build_candidate_context(
    opportunity_title: str,
    opportunity_description: str | None,
    eligibility_criteria: str | None,
    user_background: str,
    key_achievements: str | None,
    additional_context: str | None,
) -> str:
    """Shared user-turn content builder for all three document-generation
    calls. The fixed instructions/rules live in each method's system
    prompt; this is just the variable, per-request data."""
    lines = [f"Opportunity: {opportunity_title}"]
    if opportunity_description:
        lines.append(f"Opportunity description: {opportunity_description}")
    if eligibility_criteria:
        lines.append(f"Eligibility criteria: {eligibility_criteria}")
    lines.append(f"\nCandidate's background (as provided by the candidate):\n{user_background}")
    if key_achievements:
        lines.append(f"\nKey achievements / skills to emphasize:\n{key_achievements}")
    if additional_context:
        lines.append(f"\nAdditional context from the candidate:\n{additional_context}")
    return "\n".join(lines)


# Every one of these prompts repeats the same non-negotiable rule: don't
# invent facts. A generated CV or recommendation letter that fabricates a
# specific employer, date, or credential the candidate never provided isn't
# a quality problem -- if submitted in a real application, it's misrepresentation.
# Anthropic's Claude is used for these three specifically for its strength at
# natural, well-structured long-form writing.

_CV_SYSTEM_PROMPT = """\
You are an expert career advisor helping a candidate prepare a tailored CV/resume \
in Markdown for a specific opportunity.

Rules you must follow:
- Use ONLY the information given to you about the candidate. Never invent specific \
employers, job titles, dates, degrees, institutions, certifications, or numbers \
that weren't provided -- this document may be submitted as part of a real \
application, and fabricated credentials would be a serious problem for the candidate.
- If a standard CV section (e.g. "Work Experience", "Education") isn't supported \
by anything in the candidate's background, omit that section rather than filling \
it with invented content.
- Where the background is vague, keep the corresponding section general instead \
of inventing specifics to make it sound more complete.
- Tailor emphasis and framing -- not facts -- to the specific opportunity \
described: highlight the parts of the candidate's real background most relevant to it.
- Output clean Markdown with standard headings and bullet points. Do not wrap the \
whole thing in a code block.
- Do not fabricate a name, email, or phone number for the header -- use a generic \
placeholder like "[Full Name]" / "[Contact Information]" for those specific \
fields only, since the candidate didn't supply them.
"""

_MOTIVATION_LETTER_SYSTEM_PROMPT = """\
You are an expert at writing compelling, genuine-sounding motivation letters and \
personal statements for scholarships, internships, volunteer positions, and courses.

Rules you must follow:
- Use ONLY the background information given to you. Never invent specific stories, \
numbers, dates, institutions, or achievements the candidate didn't mention -- this \
letter may be submitted as part of a real application.
- Write it as flowing prose in paragraphs, the way a real letter reads -- not as \
bullet points, and not as a bare list of qualifications.
- Explicitly connect the candidate's real background to what this specific \
opportunity is looking for. Avoid a generic letter that could be sent anywhere.
- Avoid cliche opening lines ("I am writing to express my interest in...") and \
empty superlatives ("I am the perfect candidate"). Sound like a specific person, \
not a template.
- Aim for roughly 400-600 words. If the provided background is too sparse to \
responsibly reach that length without padding or fabricating, write a shorter, \
honest letter instead.
- Output clean Markdown (paragraphs; a closing signature line is fine). Do not \
wrap the whole thing in a code block.
"""

_RECOMMENDATION_LETTER_SYSTEM_PROMPT = """\
You are helping draft a recommendation letter, written in the first person from \
the recommender's own perspective, supporting an applicant for a specific opportunity.

Rules you must follow:
- Use ONLY the information given to you about what the recommender knows of the \
applicant. Never invent specific anecdotes, projects, dates, or superlative claims \
(e.g. "the best student I have ever taught") that go beyond what was actually \
provided -- this is a real letter that may be submitted as part of a real application.
- Write entirely in the first person as the recommender, addressed generically \
(e.g. "To the Selection Committee" or "To Whom It May Concern").
- If the provided context about the applicant is sparse, write a shorter, more \
general letter rather than inventing specifics to fill space.
- Close with the recommender's name and role/title exactly as given.
- Output clean Markdown as prose (paragraphs) -- a real recommendation letter \
doesn't read as a bullet list of qualifications. Do not wrap the whole thing in a \
code block.
"""


class AIGateway:
    """Async wrapper around two LLM providers:

    - OpenAI: structured-output verification (`verify_opportunity`) and
      embeddings (`generate_embedding`), unchanged from Step 2.1.
    - Anthropic (Claude): long-form document generation
      (`generate_cv`, `generate_motivation_letter`,
      `generate_recommendation_letter`) -- added in this step for its
      strength at natural, well-structured prose versus OpenAI's role here,
      which is forced-schema structured extraction.

    Both providers follow the same client lifecycle pattern: a fresh client
    is constructed per call rather than held as a long-lived attribute.
    This class is called both from Celery tasks that each run their own
    `asyncio.run()` loop (app/workers/tasks.py, Step 2.1) and from FastAPI
    request handlers running inside one persistent loop (Step 3.3's
    ai_tools endpoints) -- an httpx-backed client built in one event loop
    and reused in a different one is a known source of "Event loop is
    closed" errors. Building fresh per call is correct in both contexts,
    at a connection-setup cost that's negligible next to LLM call latency.
    """

    def _openai_client(self) -> AsyncOpenAI:
        return AsyncOpenAI(api_key=settings.openai_api_key)

    def _anthropic_client(self) -> AsyncAnthropic:
        return AsyncAnthropic(api_key=settings.anthropic_api_key)

    # ---------------------------------------------------------------
    # OpenAI: structured verification + embeddings
    # ---------------------------------------------------------------

    @_retry_openai_call
    async def verify_opportunity(self, raw_data: dict) -> VerifiedOpportunitySchema:
        async with self._openai_client() as client:
            response = await client.beta.chat.completions.parse(
                model=settings.ai_verification_model,
                messages=[
                    {"role": "system", "content": _VERIFICATION_SYSTEM_PROMPT},
                    {"role": "user", "content": self._format_raw_data(raw_data)},
                ],
                response_format=VerifiedOpportunitySchema,
                temperature=0,
            )

        choice = response.choices[0]
        if choice.message.refusal:
            raise ValueError(f"Model refused to verify opportunity: {choice.message.refusal}")
        if choice.message.parsed is None:
            raise ValueError("Model returned no parsed structured output")
        return choice.message.parsed

    @_retry_openai_call
    async def generate_embedding(self, text: str) -> list[float]:
        async with self._openai_client() as client:
            response = await client.embeddings.create(
                model=settings.ai_embedding_model,
                input=text,
            )
        return response.data[0].embedding

    async def verify_and_embed(
        self, raw_data: dict
    ) -> tuple[VerifiedOpportunitySchema, list[float]]:
        """Verify, then embed the cleaned text. Used together by the
        Celery verification task (app/workers/tasks.py)."""
        verification = await self.verify_opportunity(raw_data)

        embedding_text = (
            " ".join(
                part
                for part in (
                    verification.title,
                    verification.eligibility_criteria,
                    verification.funding_details,
                )
                if part
            )
            or verification.title
        )
        embedding = await self.generate_embedding(embedding_text)
        return verification, embedding

    @staticmethod
    def _format_raw_data(raw_data: dict) -> str:
        return f"Raw scraped data (JSON):\n{json.dumps(raw_data, indent=2, default=str)}"

    # ---------------------------------------------------------------
    # Anthropic: document generation
    # ---------------------------------------------------------------

    @_retry_anthropic_call
    async def _generate_document(self, system_prompt: str, user_content: str) -> str:
        async with self._anthropic_client() as client:
            response = await client.messages.create(
                model=settings.anthropic_document_model,
                max_tokens=settings.anthropic_max_tokens,
                temperature=settings.anthropic_document_temperature,
                system=system_prompt,
                messages=[{"role": "user", "content": user_content}],
            )

        if response.stop_reason == "max_tokens":
            logger.warning(
                "Document generation hit max_tokens (%s) -- output may be truncated",
                settings.anthropic_max_tokens,
            )

        text_blocks = [block.text for block in response.content if block.type == "text"]
        if not text_blocks:
            raise ValueError("Claude returned no text content for document generation")
        return "\n".join(text_blocks).strip()

    async def generate_cv(
        self,
        *,
        opportunity_title: str,
        opportunity_description: str | None,
        eligibility_criteria: str | None,
        user_background: str,
        key_achievements: str | None = None,
        additional_context: str | None = None,
    ) -> str:
        context = _build_candidate_context(
            opportunity_title,
            opportunity_description,
            eligibility_criteria,
            user_background,
            key_achievements,
            additional_context,
        )
        return await self._generate_document(_CV_SYSTEM_PROMPT, context)

    async def generate_motivation_letter(
        self,
        *,
        opportunity_title: str,
        opportunity_description: str | None,
        eligibility_criteria: str | None,
        user_background: str,
        key_achievements: str | None = None,
        additional_context: str | None = None,
    ) -> str:
        context = _build_candidate_context(
            opportunity_title,
            opportunity_description,
            eligibility_criteria,
            user_background,
            key_achievements,
            additional_context,
        )
        return await self._generate_document(_MOTIVATION_LETTER_SYSTEM_PROMPT, context)

    async def generate_recommendation_letter(
        self,
        *,
        opportunity_title: str,
        opportunity_description: str | None,
        eligibility_criteria: str | None,
        user_background: str,
        recommender_name: str,
        recommender_role: str,
        key_achievements: str | None = None,
        additional_context: str | None = None,
    ) -> str:
        base_context = _build_candidate_context(
            opportunity_title,
            opportunity_description,
            eligibility_criteria,
            user_background,
            key_achievements,
            additional_context,
        )
        context = f"Recommender: {recommender_name} ({recommender_role})\n\n{base_context}"
        return await self._generate_document(_RECOMMENDATION_LETTER_SYSTEM_PROMPT, context)


# Module-level singleton -- stateless (each call builds its own client), so
# sharing this instance across tasks/requests is safe.
ai_gateway = AIGateway()
