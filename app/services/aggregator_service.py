"""Hybrid Opportunity Aggregation Engine.

A background pipeline that reads a JSON config of Telegram channels and
Facebook pages, "fetches" their new posts, runs each raw post through a
Gemini structured-extraction call, and upserts the result into the
existing `opportunities` table -- reusing this codebase's existing
`Opportunity`/`Source` schema rather than inventing a parallel one. See
`app/models/opportunity.py` and `app/models/source.py`'s own module
docstrings for the full disclosed reasoning behind every schema decision
this pipeline depends on (why `source_name`/`source_type` live on
`Source`, not `Opportunity`; why "original_link" is `target_url`; why
`country` now defaults to `"Global"`).

RELATIONSHIP TO THE PRE-EXISTING SCRAPER PIPELINE (`app/scraper/pipelines.py`
+ `app/workers/tasks.py`): this codebase already has a website-scraping
pipeline (Scrapy) that inserts `Opportunity` rows with `is_verified=False,
ai_confidence_score=None`, and a SEPARATE periodic Celery task
(`verify_new_opportunities`) that later confirms legitimacy and generates
the semantic-search embedding for any row still in that state. THIS
pipeline deliberately reuses that same two-phase design rather than
building a second, parallel verification/embedding path: every
`Opportunity` row this pipeline creates is ALSO left at `is_verified=False,
ai_confidence_score=None, embedding=None` on insert, so it's picked up by
the exact same `verify_new_opportunities` task on its own next run, with
zero changes needed to that task (its own query has no source-type
filter). This is a disclosed, deliberate design choice: this step's own
ask doesn't mention embeddings or a second confidence-scored legitimacy
check at all, and building a duplicate mechanism for something this
codebase already does well would be redundant, not thorough.

DISCLOSED NECESSARY ADDITION beyond this step's own literal AI-extraction
field list (Title/Description/Deadline/Link/Target Age Group/is_local_egypt):
`Opportunity.type` (`OpportunityType`) is a real, pre-existing `NOT NULL`
column with no default -- every insert needs SOME valid value for it, and
it has nothing to do with local/global classification. `process_raw_post_via_ai`'s
own Gemini prompt therefore also asks for a best-fit classification into
this codebase's four existing categories (scholarship/internship/
volunteer/course), with a disclosed, defensive `COURSE` fallback
(`_FALLBACK_OPPORTUNITY_TYPE`) if Gemini's answer doesn't map cleanly onto
any of them -- chosen as the least presumptuous fallback (a generic
"course/training" bucket) rather than guessing "scholarship" for content
that may not be one at all.
"""

import json
import logging
from datetime import datetime
from pathlib import Path

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.models.enums import OpportunityType, SourceType
from app.models.opportunity import Opportunity
from app.models.source import Source
from app.schemas.opportunity import OpportunityCreate
from app.services.taxonomy import normalize_country

logger = logging.getLogger(__name__)

# Same retry posture as every other Gemini-backed service in this
# codebase (e.g. `student_video_service.py`, `app/workers/scholar_agent.py`).
_retry_gemini_call = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(genai_errors.APIError),
)

# The least presumptuous fallback when Gemini's own `opportunity_type`
# guess doesn't map onto one of this codebase's four real categories --
# see module docstring.
_FALLBACK_OPPORTUNITY_TYPE = OpportunityType.COURSE


class AggregatorError(Exception):
    """Raised when the Gemini extraction call itself fails or returns
    unusable output -- distinct from a single malformed FIELD inside an
    otherwise-successful response (handled by defensive fallbacks
    instead, e.g. `_coerce_opportunity_type`)."""


# ---------------------------------------------------------------------
# Sources config (component 2's "loads a JSON config file")
# ---------------------------------------------------------------------


class TelegramChannelConfig(BaseModel):
    handle: str = Field(min_length=1, description="e.g. '@tadrebat_1', including the leading @.")
    category: str = Field(min_length=1, description="e.g. 'Scholarships', 'Internships_Training'.")


class FacebookPageConfig(BaseModel):
    page_id: str = Field(min_length=1, description="e.g. 'ITIEgypt'.")
    name: str = Field(min_length=1, description="Human-readable page name (often Arabic).")
    target_age: str | None = Field(
        default=None, description="e.g. 'prep_secondary', 'university', 'graduates', 'all'."
    )


class SourcesConfig(BaseModel):
    telegram_channels: list[TelegramChannelConfig] = Field(default_factory=list)
    facebook_pages: list[FacebookPageConfig] = Field(default_factory=list)


def _resolve_config_path(path: str | Path | None = None) -> Path:
    """Resolves `settings.opportunity_sources_config_path` (or an
    explicit override, mainly for tests) relative to this FILE's own
    location, not the process's current working directory -- see that
    setting's own comment in app/core/config.py for why."""
    raw = path if path is not None else settings.opportunity_sources_config_path
    candidate = Path(raw)
    if candidate.is_absolute():
        return candidate
    # app/services/aggregator_service.py -> parents[2] is the backend
    # project root (the directory containing `app/`).
    project_root = Path(__file__).resolve().parents[2]
    return project_root / candidate


def load_sources_config(path: str | Path | None = None) -> SourcesConfig:
    """Loads and validates the JSON config listing this pipeline's
    Telegram channels / Facebook pages (`app/data/opportunity_sources.json`
    by default). Raises `FileNotFoundError`/`pydantic.ValidationError`
    directly on failure -- deliberately NOT swallowed here, since a
    missing or malformed sources config means this entire pipeline run
    has nothing to do; the caller (the Celery task) is the right place to
    decide how to log/report that, not this loader.
    """
    resolved_path = _resolve_config_path(path)
    raw_json = resolved_path.read_text(encoding="utf-8")
    return SourcesConfig.model_validate(json.loads(raw_json))


# ---------------------------------------------------------------------
# Mocked ingestion (component 2's "mocked external fetching logic")
# ---------------------------------------------------------------------
#
# DISCLOSED SCOPE: per this step's own explicit instruction ("use mocked
# external fetching logic for now"), no real Telegram Bot API / Facebook
# Graph API client is implemented here. A real implementation would need
# actual API credentials (a Telegram bot token with channel access, a
# Facebook Graph API app token with `pages_read_engagement`) that aren't
# available in this environment, plus per-platform pagination/rate-limit
# handling -- explicitly out of scope for this step, called out as a
# "Not included" item.
#
# The dummy text below is hand-written to plausibly resemble a real post
# from each of this step's own literal example channels (`@tadrebat_1`,
# `@grabscholarship`), mixing Arabic and English exactly the way real
# Egyptian opportunity channels typically do -- this is what makes the
# verification pytest suite's "parses a sample Arabic Telegram post"
# requirement meaningful to test against.

_DUMMY_TELEGRAM_POSTS: dict[str, list[str]] = {
    "@tadrebat_1": [
        "تدريب صيفي مدفوع الأجر في شركة فودافون مصر لخريجي وطلاب كلية الحاسبات "
        "والمعلومات! التدريب لمدة 3 شهور بمرتب شهري. آخر موعد للتقديم 30 أغسطس 2026. "
        "قدم الآن من خلال الرابط: https://careers.vodafone.com.eg/summer-internship-2026"
    ],
    "@grabscholarship": [
        "Fully-funded DAAD Scholarship for Master's students in Engineering -- "
        "covers tuition, monthly stipend, and travel costs. Open to applicants "
        "worldwide. Deadline: 2026-10-15. Apply here: https://www.daad.de/en/study-and-research-in-germany/scholarships/"
    ],
}

_GENERIC_DUMMY_TELEGRAM_POST = (
    "فرصة تدريبية جديدة متاحة الآن لطلاب الجامعات المصرية! التفاصيل والتقديم عبر "
    "الرابط المرفق. لا تفوت الفرصة، العدد محدود."
)

_GENERIC_DUMMY_FACEBOOK_POST = (
    "إعلان هام: فرصة "
    "تدريبية جديدة متاحة "
    "الآن للطلاب والخريجين. "
    "للتقديم متابعة الرابط في التعليقات."
)


def fetch_new_telegram_posts(channel: TelegramChannelConfig) -> list[str]:
    """MOCKED fetch -- see module section docstring. Returns dummy raw
    text strings simulating what a real Telegram Bot API `getUpdates`/
    channel-history call would return for `channel`, keyed by handle for
    this step's own two literal example channels, falling back to a
    generic dummy post (still mentioning the channel's own `category`
    hint) for every other configured channel."""
    if channel.handle in _DUMMY_TELEGRAM_POSTS:
        return list(_DUMMY_TELEGRAM_POSTS[channel.handle])
    return [f"[{channel.category}] {_GENERIC_DUMMY_TELEGRAM_POST}"]


def fetch_new_facebook_posts(page: FacebookPageConfig) -> list[str]:
    """MOCKED fetch -- see module section docstring. Returns a dummy raw
    text string simulating what a real Facebook Graph API `/feed` call
    would return for `page`."""
    return [f"[{page.name}] {_GENERIC_DUMMY_FACEBOOK_POST}"]


# ---------------------------------------------------------------------
# AI extraction (component 2's "AI Processing Pipeline")
# ---------------------------------------------------------------------


class AIExtractedOpportunityFields(BaseModel):
    """Structured output Gemini's extraction call is forced to produce,
    via `response_schema` -- same mechanism as every other Gemini
    structured-output call in this codebase (e.g.
    `student_video_service.GeminiVideoEvaluation`), so there's no
    hand-rolled JSON parsing here.

    Exactly this step's own literal five extraction fields, PLUS the
    disclosed `opportunity_type` addition -- see module docstring.
    `original_link`/`deadline` are both `str | None` (not `HttpUrl`/
    `datetime`) because a raw social-media post frequently doesn't
    contain a literal link or an ISO-formatted date at all; forcing a
    stricter type here would make Gemini's own JSON generation itself
    more likely to fail structurally on exactly the sparse/informal posts
    this pipeline exists to handle. Both are parsed/defaulted
    defensively AFTER the structured call succeeds
    (`process_raw_post_via_ai`), not validated as strict types up front.
    """

    # Backfill Real Eligibility Data step -- DISCLOSED, deliberately NOT
    # extended with min_age/max_age/target_gender/target_academic_levels
    # here. See app/workers/tasks.py::verify_new_opportunities's own
    # docstring for the full reasoning: that function's downstream
    # verification call is the real single funnel every Opportunity row
    # (from this pipeline AND the separate Scrapy pipeline) passes
    # through, and the only place with real retained raw source text for
    # backfilling existing rows -- extraction lives there instead.
    title: str = Field(description="A clean, human-readable title for the opportunity.")
    description: str | None = Field(
        default=None, description="A cleaned summary of the post's Arabic/English text."
    )
    deadline: str | None = Field(
        default=None, description="Application deadline in ISO 8601 format, or null if not stated."
    )
    original_link: str | None = Field(
        default=None,
        description="The exact application/original link mentioned in the post, or null if none is present.",
    )
    target_age_group: str | None = Field(
        default=None,
        description="e.g. 'prep_secondary', 'university', 'graduates', or null if not determinable.",
    )
    country_hint: str | None = Field(
        default=None,
        description=(
            "The country or geographic scope EXPLICITLY supported by the post's own text "
            "(e.g. 'Egypt', 'France', 'Kenya', 'United States', 'United Kingdom', 'Global', "
            "'Online'), or null if the post states no geography at all. D5.1 (Global Country "
            "Extraction Unblock): this replaced the earlier Egypt-only `is_local_egypt: bool` "
            "field, which structurally capped every extraction at Egypt-or-Global. This is a "
            "discovery/extraction signal only -- it must NEVER be guessed from language, "
            "organization name, or source-channel origin (see the system prompt's own "
            "no-guessing rule), and it establishes no legitimacy/eligibility/verification "
            "status; `Opportunity.is_verified` stays independently False on every insert this "
            "module makes, exactly as before (see `upsert_opportunity`)."
        ),
    )
    opportunity_type: str = Field(
        description="Best-fit classification: one of 'scholarship', 'internship', 'volunteer', 'course'."
    )


_EXTRACTION_SYSTEM_PROMPT = """\
Analyze this social media post. Clean the Arabic/English text. Extract the \
opportunity Title, Description, Deadline (ISO format), Original Application \
Link, Target Age Group, and the country or geographic scope the post's own \
text explicitly supports (country_hint). Return valid JSON.

You must return a strict JSON object with these exact keys:
- 'title': string, a clean, human-readable title.
- 'description': string or null, a cleaned summary of the post's content.
- 'deadline': string or null, ISO 8601 format (YYYY-MM-DD), null if no \
deadline is stated.
- 'original_link': string or null, the exact application link mentioned in \
the post, null if none is present. Never invent a link that isn't in the text.
- 'target_age_group': string or null, e.g. 'prep_secondary', 'university', \
'graduates', null if not determinable from the post.
- 'country_hint': string or null, the specific country or geographic scope \
(e.g. 'Egypt', 'France', 'Kenya', 'United States', 'United Kingdom', \
'Global', 'Online') that the post's own text EXPLICITLY states or clearly \
supports. Do NOT guess a country from the language the post is written in, \
from an organization's name sounding like it belongs to a particular \
country, or from which channel/page the post came from -- extract only \
what the post's own content actually says. If the post explicitly says the \
opportunity is global/international/worldwide/remote/online, return \
'Global'. If no geographic scope can be determined from the post's own \
text, return null.
- 'opportunity_type': string, your best-fit classification of this \
opportunity as exactly one of: 'scholarship', 'internship', 'volunteer', \
'course'. Use 'course' for training programs, workshops, or general \
self-development content that doesn't clearly fit the other three.
"""


def _build_extraction_user_prompt(raw_text: str, source_info: dict) -> str:
    """Includes whatever hint fields `source_info` actually has (this
    channel's/page's own `category`/`target_age`, if any) as CONTEXT for
    the model, not as a hard override of its own per-post judgment -- a
    channel broadly categorized "Scholarships" can still occasionally
    post something else, and a Facebook page whose stated `target_age`
    is "graduates" can still post something relevant to university
    students. The model makes the final per-post call either way.
    """
    lines = [f"Raw post text:\n{raw_text}"]
    category = source_info.get("category")
    if category:
        lines.append(f"\nThis source's general category hint: {category}")
    target_age_hint = source_info.get("target_age")
    if target_age_hint:
        lines.append(f"\nThis source's general target-age hint: {target_age_hint}")
    return "\n".join(lines)


def _gemini_client() -> genai.Client:
    return genai.Client(api_key=settings.gemini_api_key)


@_retry_gemini_call
async def _extract_fields_via_gemini(raw_text: str, source_info: dict) -> AIExtractedOpportunityFields:
    try:
        response = await _gemini_client().aio.models.generate_content(
            model=settings.gemini_opportunity_aggregator_model,
            # `# type: ignore[arg-type]` -- disclosed, scoped mypy fix: the
            # google-genai SDK's own type stubs for `contents` don't
            # cleanly cover a plain `list[str]`, even though this is the
            # SAME `contents=[SYSTEM_PROMPT, user_content]` call shape
            # already used identically (and working correctly at runtime)
            # in every other Gemini-backed service in this codebase, e.g.
            # `student_video_service.py`/`app/workers/scholar_agent.py`.
            # This milestone's own mypy run is scoped to ONLY this
            # milestone's new/changed files (see README/verification
            # notes), so this ignore is added here rather than silently
            # leaving a real-looking error in a NEW file -- it is not a
            # retroactive fix applied to those other, pre-existing files.
            contents=[_EXTRACTION_SYSTEM_PROMPT, _build_extraction_user_prompt(raw_text, source_info)],  # type: ignore[arg-type]
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=AIExtractedOpportunityFields,
            ),
        )
    except genai_errors.APIError:
        raise
    except Exception as exc:
        raise AggregatorError(str(exc)) from exc

    if not response.text:
        raise AggregatorError("Gemini returned no content for opportunity extraction")

    try:
        return AIExtractedOpportunityFields.model_validate_json(response.text)
    except ValidationError as exc:
        raise AggregatorError(f"Gemini returned malformed structured output: {exc}") from exc


def _coerce_opportunity_type(raw_value: str) -> OpportunityType:
    try:
        return OpportunityType(raw_value.strip().lower())
    except ValueError:
        logger.warning(
            "Gemini returned unrecognized opportunity_type %r -- falling back to %r",
            raw_value,
            _FALLBACK_OPPORTUNITY_TYPE.value,
        )
        return _FALLBACK_OPPORTUNITY_TYPE


def _parse_deadline(raw: str | None) -> datetime | None:
    """Same ISO-8601-with-trailing-Z normalization as every other AI-fed
    deadline parser in this codebase (`app/scraper/pipelines.py`,
    `app/workers/tasks.py`) -- `datetime.fromisoformat` only accepts a
    bare trailing 'Z' starting on Python 3.11+, so it's normalized here
    to stay correct regardless of the running interpreter version."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        logger.warning("Could not parse AI-returned deadline %r", raw)
        return None


def _resolve_target_url(ai_link: str | None, source_base_url: str) -> str:
    """`Opportunity.target_url` is a real `NOT NULL` column. A raw social
    post frequently has no literal link in it at all (this step's own ask
    acknowledges the model may return null here) -- rather than crash the
    whole pipeline run or fabricate a fake URL, this falls back to the
    SOURCE's own base URL (the channel/page itself), so the resulting
    Opportunity still points somewhere real a student could actually
    follow up on."""
    if ai_link and ai_link.strip():
        return ai_link.strip()
    return source_base_url


async def process_raw_post_via_ai(raw_text: str, source_info: dict) -> OpportunityCreate:
    """This step's own explicitly-named function. Constructs the prompt
    specified above, forces Gemini's structured JSON output, and maps it
    into `OpportunityCreate` -- merging the AI-extracted per-post fields
    with `source_info`'s own per-source facts (name/type/base_url), which
    the model is never asked to guess.

    `source_info` is a plain `dict` (per this step's own literal function
    signature) with keys: `source_name`, `source_type` (a `SourceType`
    value), `source_base_url`, and optionally `category`/`target_age`
    (passed through to the prompt as context hints -- see
    `_build_extraction_user_prompt`).
    """
    extracted = await _extract_fields_via_gemini(raw_text, source_info)

    # D5.1 (Global Country Extraction Unblock): `country_hint` is the raw,
    # country-neutral text the extraction step produced (see
    # `AIExtractedOpportunityFields.country_hint`'s own docstring) -- no
    # Egypt-specific branching happens here. `Opportunity.country` keeps
    # its existing, disclosed posture (free text, "Global" fallback when
    # nothing is stated -- see module docstring) rather than introducing a
    # third null/"Unknown" state this column has never held before.
    # `is_local_egypt` (the real, indexed DB column the student dashboard's
    # own fast-boolean filter depends on -- see `dashboards.py`) is then
    # DERIVED downstream from that same value via the existing
    # `taxonomy.normalize_country` layer, rather than being a primary
    # extraction signal -- it is exactly the "Egypt" special case of a now
    # country-neutral pipeline, not the pipeline's ceiling.
    country = extracted.country_hint.strip() if extracted.country_hint and extracted.country_hint.strip() else "Global"
    is_local_egypt = normalize_country(country) == "EG"

    return OpportunityCreate(
        title=extracted.title,
        description=extracted.description,
        target_url=_resolve_target_url(extracted.original_link, source_info["source_base_url"]),
        opportunity_type=_coerce_opportunity_type(extracted.opportunity_type),
        deadline=_parse_deadline(extracted.deadline),
        country=country,
        is_local_egypt=is_local_egypt,
        target_age_group=extracted.target_age_group,
        source_name=source_info["source_name"],
        source_type=source_info["source_type"],
        source_base_url=source_info["source_base_url"],
    )


# ---------------------------------------------------------------------
# Persistence: Source get-or-create + Opportunity upsert (component 3.4)
# ---------------------------------------------------------------------


async def get_or_create_source(
    db: AsyncSession, *, name: str, base_url: str, source_type: SourceType
) -> Source:
    """Same get-or-create-by-`name` pattern as `app/scraper/pipelines.py`'s
    `PostgresPipeline.open_spider` -- reused rather than re-invented, since
    it already correctly handles "first time we've ever seen this source"
    vs. "we already have a Source row for it." Deliberately does NOT
    overwrite an existing row's `source_type`/`base_url` if they differ
    from what's passed in -- a non-destructive read-mostly lookup, not a
    sync operation; changing an existing Source's recorded platform/URL is
    a decision for a human, not something this pipeline should do silently
    on every run.
    """
    result = await db.execute(select(Source).where(Source.name == name))
    source = result.scalar_one_or_none()
    if source is not None:
        return source

    source = Source(name=name, base_url=base_url, source_type=source_type.value, active_status=True)
    db.add(source)
    await db.flush()
    return source


async def upsert_opportunity(db: AsyncSession, data: OpportunityCreate) -> tuple[Opportunity, bool]:
    """This step's own explicit "upsert logic (avoiding duplicates based
    on original_link)" requirement -- a TRUE upsert (update if a row with
    this `target_url` already exists, insert otherwise), not just a
    skip-on-duplicate guard (which is what the pre-existing Scrapy
    pipeline's own de-dupe does -- see that pipeline's own docstring for
    why THAT pipeline's simpler posture was the right call for a
    once-per-crawl website scrape; a daily-recurring social-media
    aggregator re-fetching the SAME post is a more realistic occurrence,
    where refreshing an already-seen listing's content is more useful
    than silently ignoring it).

    De-dupe key is a PLAIN `target_url` match, not scoped to `source_id`
    the way the Scrapy pipeline's own guard is -- deliberate, disclosed
    deviation: the exact same opportunity link can legitimately be
    reposted by more than one Telegram channel/Facebook page, and a
    global de-dupe is the more correct behavior for that case (one real
    opportunity, one row, regardless of how many sources mentioned it).

    On UPDATE, only the content fields this pipeline could plausibly have
    fresher data for are refreshed (title/description/target_url/type/
    deadline/country/is_local_egypt/target_age_group). `source_id`,
    `is_verified`, `ai_confidence_score`, and `embedding` are deliberately
    LEFT UNTOUCHED on an update -- an opportunity that was already
    verified/embedded by `verify_new_opportunities` should not be silently
    reset back into the review queue just because this pipeline saw the
    same link again.

    Returns `(opportunity, was_created)` so the calling Celery task can
    report accurate created-vs-updated counts.
    """
    result = await db.execute(select(Opportunity).where(Opportunity.target_url == data.target_url))
    existing = result.scalar_one_or_none()

    if existing is not None:
        existing.title = data.title
        existing.description = data.description
        existing.type = data.opportunity_type
        existing.deadline = data.deadline
        existing.country = data.country
        existing.is_local_egypt = data.is_local_egypt
        existing.target_age_group = data.target_age_group
        return existing, False

    source = await get_or_create_source(
        db, name=data.source_name, base_url=data.source_base_url, source_type=data.source_type
    )

    opportunity = Opportunity(
        title=data.title,
        description=data.description,
        target_url=data.target_url,
        source_id=source.id,
        type=data.opportunity_type,
        deadline=data.deadline,
        country=data.country,
        is_local_egypt=data.is_local_egypt,
        target_age_group=data.target_age_group,
        # Left at their model defaults deliberately -- see module
        # docstring: this row now enters the SAME `is_verified=False`
        # review queue `verify_new_opportunities` already polls.
        is_verified=False,
        ai_confidence_score=None,
        raw_extracted_data=data.model_dump(mode="json"),
        embedding=None,
    )
    db.add(opportunity)
    await db.flush()
    return opportunity, True
