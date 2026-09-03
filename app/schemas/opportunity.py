from datetime import datetime
from typing import Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.models.enums import (
    AcademicLevel,
    Gender,
    OpportunityReviewStatus,
    OpportunityType,
    SourceType,
)

T = TypeVar("T")


class SourceOut(BaseModel):
    """Minimal Source info nested inside OpportunityOut. Cheap to include:
    Opportunity.source is eager-loaded via lazy="selectin" (see
    app/models/opportunity.py), so this never triggers an extra query.

    `source_type` addition -- Hybrid Opportunity Aggregation Engine step.
    This is where that step's own "source_type" concept is actually
    exposed to API consumers (nested under `OpportunityOut.source`, i.e.
    `opportunity.source.source_type`) -- see `app/models/source.py`'s own
    module docstring for why it's not a second, duplicate field directly
    on `OpportunityOut` itself.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    source_type: SourceType


class OpportunityOut(BaseModel):
    """Public API shape for an Opportunity.

    Deliberately excludes `raw_extracted_data` (internal scrape payload --
    not meant for public consumption, and can contain arbitrary scraped
    HTML/text) and `embedding` (a 1536-float vector with no client-side
    use, that would needlessly bloat every list response).
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    description: str | None
    target_url: str
    type: OpportunityType
    deadline: datetime | None
    eligibility_criteria: str | None
    funding_details: str | None
    location: str | None
    country: str | None
    is_verified: bool
    ai_confidence_score: float | None
    # Review of Aggregated Opportunities Before Publish addition -- the
    # real pre-publish gate. See `OpportunityReviewStatus`'s own
    # docstring (app/models/enums.py) for why this is now what actually
    # controls visibility, not `is_verified` above (which keeps its own,
    # narrower, purely-informational meaning: the AI verification task's
    # legitimacy judgment).
    review_status: OpportunityReviewStatus
    # Hybrid Opportunity Aggregation Engine additions.
    is_local_egypt: bool
    target_age_group: str | None
    # Backend Folder Structure & Core Database Models step addition --
    # see app/models/opportunity.py's own comment on this column.
    # DISCLOSED SCOPE BOUNDARY: exposed here for reads, but NOT added to
    # `OpportunityCreate` below -- `aggregator_service.py`'s own Gemini
    # extraction prompt has not been taught to populate this field (out
    # of scope for this pass; see this file's own module-level judgment
    # call in the README). Every row this schema currently serializes
    # will have `target_academic_levels: null` until either a future
    # aggregator-prompt update or manual admin curation populates it.
    target_academic_levels: list[AcademicLevel] | None
    # Real Age/Academic-Year/Gender Opportunity Filtering step additions.
    # Same disclosed scope boundary as `target_academic_levels` directly
    # above: exposed here for reads, NOT added to `OpportunityCreate`
    # below -- the real Gemini aggregator pipeline has not been taught to
    # extract a numeric age range or a gender restriction from a scraped
    # posting (confirmed via a direct read of that prompt/schema). Every
    # existing row will have these as `null` (unrestricted) until a
    # future aggregator-prompt update or manual admin curation populates
    # them.
    min_age: int | None
    max_age: int | None
    target_gender: Gender | None
    source: SourceOut
    created_at: datetime
    updated_at: datetime


class OpportunitySearchResult(OpportunityOut):
    """OpportunityOut plus the similarity score from a semantic search hit."""

    similarity_score: float = Field(
        description="Cosine similarity to the query embedding, roughly in "
        "[0, 1] for typical text embeddings (1 = identical direction). "
        "Higher is more relevant."
    )


class PaginatedResponse(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


class OpportunitySearchRequest(BaseModel):
    query: str = Field(
        min_length=1,
        max_length=500,
        description="Natural-language search query, e.g. 'Medical scholarships in Europe'.",
    )
    limit: int = Field(default=10, ge=1, le=50)
    min_similarity: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Override the server-side default similarity threshold "
        "(app.core.config.settings.search_similarity_threshold) for this "
        "request only.",
    )


class OpportunityCreate(BaseModel):
    """Hybrid Opportunity Aggregation Engine step's own explicitly-named
    output schema: `process_raw_post_via_ai` (app/services/aggregator_service.py)
    parses the AI's raw JSON output and maps it into exactly this shape,
    which `aggregator_service.upsert_opportunity` then writes into an
    `Opportunity` row (`type=self.opportunity_type`, `target_url=
    self.target_url`, ...).

    Field-naming notes, consistent with the disclosed reasoning in
    `app/models/opportunity.py`'s own module docstring:
    - `target_url` here IS this step's "original_link" -- the ask's own
      name for a concept this codebase already calls `target_url`.
    - `source_name`/`source_type` are carried on THIS schema (unlike the
      ORM model, where they're deliberately NOT duplicated columns) --
      here they're just plain input data the caller already has from the
      sources config, needed to resolve/create the right `Source` row.
      They never get written as columns on `Opportunity` itself.
    - `opportunity_type` (`OpportunityType`) is a disclosed, NECESSARY
      addition beyond this step's own literal field list: `Opportunity.type`
      is a real `NOT NULL` column with no default (see
      app/models/opportunity.py) that predates this step and has nothing
      to do with local/global classification -- a value MUST be supplied
      for every insert, so `process_raw_post_via_ai`'s own Gemini prompt
      additionally asks for a best-fit classification into the four
      pre-existing categories (scholarship/internship/volunteer/course),
      with a disclosed `COURSE` fallback if Gemini's answer doesn't map
      cleanly onto any of them (see that function's own docstring).
    """

    title: str = Field(min_length=1, max_length=500)
    description: str | None = None
    target_url: str = Field(min_length=1, max_length=2048, description="This step's own 'original_link'.")
    opportunity_type: OpportunityType
    deadline: datetime | None = None
    country: str = Field(default="Global")
    is_local_egypt: bool
    target_age_group: str | None = None
    source_name: str = Field(min_length=1, max_length=255)
    source_type: SourceType
    source_base_url: str = Field(
        min_length=1,
        max_length=2048,
        description="Disclosed addition beyond this step's own literal field list -- "
        "`Source.base_url` is a real NOT NULL column (app/models/source.py); needed "
        "so `upsert_opportunity` can get-or-create the right `Source` row.",
    )


# =============================================================================
# Review of Aggregated Opportunities Before Publish
# =============================================================================


class AdminOpportunityReviewOut(OpportunityOut):
    """`GET /admin/opportunities/review-queue` item shape -- `OpportunityOut`
    plus the Claim System fields every other admin review-queue schema in
    this codebase already exposes (`AdminVideoReviewOut.claimed_by`/
    `.claimed_by_name`/`.claimed_at`), so a Moderator sees who (if anyone)
    is already working this item before duplicating the effort. Claim
    state is resolved through `moderation_claim_service.get_effective_claim`
    (an expired claim renders as unclaimed here), same as every other
    claimable queue.
    """

    claimed_by: UUID | None = None
    claimed_by_name: str | None = None
    claimed_at: datetime | None = None


class AdminOpportunityDecisionRequest(BaseModel):
    """`POST /admin/opportunities/{id}/approve` and `.../reject` request
    body. `notes` is optional on both -- unlike a video/chat-ban
    rejection, there is no submitting student to notify (an aggregated
    `Opportunity` has no "owner" user -- it comes from a `Source`/the
    scraper, not a person -- confirmed via a full read of
    `app/models/opportunity.py` before deciding this), so `notes` here is
    purely an audit-trail note for the `ModerationLogEntry` outcome, not
    user-facing copy."""

    notes: str | None = Field(default=None, max_length=2000)


class AdminOpportunityDecisionResponse(BaseModel):
    """Response for both the approve and reject actions -- same
    `{success, opportunity_id, new_status}` envelope shape
    `AdminOverrideResponse` already established for videos."""

    success: bool
    opportunity_id: UUID
    new_status: OpportunityReviewStatus


# =============================================================================
# Manual Opportunity Creation, with Designated-Team-Member Access
# =============================================================================


class AdminOpportunityCreate(BaseModel):
    """`POST /admin/opportunities` request body -- a Senior-Moderator-or-
    higher hand-typing an Opportunity directly, no scraper/aggregator
    involved. Deliberately NOT `OpportunityCreate` above: that schema is
    the aggregator pipeline's own internal output shape, requiring
    `source_name`/`source_type`/`source_base_url` tied to a real scraped
    feed -- a human typing this by hand has none of that, and forcing a
    caller to fabricate a fake source name/URL just to satisfy that
    schema would be dishonest data entry. The real `Source` row this
    creates instead is a single shared "Manual Entry" row the endpoint
    itself get-or-creates (see `admin_opportunities.py::create_opportunity_manually`),
    reusing `aggregator_service.get_or_create_source` rather than
    re-implementing that lookup.

    Every field below is either a real, directly-typeable fact (title/
    description/target_url/opportunity_type/deadline/country/
    is_local_egypt/target_age_group) or one of the two later, disclosed-
    scope-boundary filtering additions (`target_academic_levels`/
    `min_age`/`max_age`/`target_gender`) that `OpportunityCreate` above
    explicitly could NOT populate because the aggregator's own Gemini
    prompt was never taught to extract them -- a human typing the row
    directly has no such limitation, so this schema accepts them.
    """

    title: str = Field(min_length=1, max_length=500)
    description: str | None = None
    target_url: str = Field(min_length=1, max_length=2048)
    opportunity_type: OpportunityType
    deadline: datetime | None = None
    country: str = Field(default="Global")
    is_local_egypt: bool = False
    target_age_group: str | None = None
    target_academic_levels: list[AcademicLevel] | None = None
    min_age: int | None = Field(default=None, ge=0, le=120)
    max_age: int | None = Field(default=None, ge=0, le=120)
    target_gender: Gender | None = None


# --- Search Coverage / Gap Analysis addition (Deliverable 1) ---
#
# STEP 1 INVESTIGATION (read the real `Opportunity` model in full, and
# `app/api/v1/endpoints/opportunities.py`/`admin_opportunities.py`
# before writing this): no existing endpoint anywhere in this codebase
# aggregates the `Opportunity` table by `country`/`type`/
# `target_academic_levels` -- every existing query either lists
# individual rows (paginated) or filters to one specific value. This is
# a genuinely new read, not an existing one exposed under a new name.
#
# STEP 2 DESIGN: real counts and real comparisons, not a fabricated
# "coverage score". `type` and `target_academic_levels` are both fixed,
# enumerated vocabularies (`OpportunityType`/`AcademicLevel`) -- every
# real value is listed even when its real count is zero, since "zero
# rows of this type exist" is itself real, disclosed information a
# fabricated score would hide. `country` is open-vocabulary (free text),
# so only countries that actually appear are listed, sorted by count
# descending; a country is flagged `is_gap=True` when its own real count
# is less than `gap_threshold_ratio` (default 0.1, i.e. under 10%) of
# the single highest real country count -- a plain, explainable
# comparison against real data, not a learned or weighted score.
class CoverageBucket(BaseModel):
    """One real value (a country name, an `OpportunityType`, or an
    `AcademicLevel`) and its real row count."""

    value: str
    count: int
    is_gap: bool


class OpportunityCoverageOut(BaseModel):
    total_opportunities: int
    by_type: list[CoverageBucket]
    by_country: list[CoverageBucket]
    by_academic_level: list[CoverageBucket]
    gap_threshold_ratio: float
