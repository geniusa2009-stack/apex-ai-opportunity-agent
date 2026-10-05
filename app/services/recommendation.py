import logging
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import OpportunityReviewStatus
from app.models.opportunity import Opportunity
from app.services.opportunity_state_machine import USER_FACING_EXCLUDED_LIFECYCLE_STATES

logger = logging.getLogger(__name__)

_SHORT_DESCRIPTION_MAX_CHARS = 200


class RecommendationSourceNotFoundError(Exception):
    """Raised when `opportunity_id` doesn't match any row. The router
    maps this to a 404."""


class RecommendationSourceNotEmbeddedError(Exception):
    """Raised when the source opportunity has no stored embedding to
    compare against -- there's nothing to run a pgvector similarity
    query off of. The router maps this to a 422."""


class SimilarOpportunity(BaseModel):
    """Deliberately lean, not the full `OpportunityOut` shape (Step 3.1) --
    the ask specifically wants id/title/short description/similarity
    score for a "you might also like" list, not every field of the full
    opportunity record."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    short_description: str | None = Field(
        description="The opportunity's description, truncated to "
        f"{_SHORT_DESCRIPTION_MAX_CHARS} characters (at a word boundary) "
        "for a compact recommendation list."
    )
    similarity_score: float = Field(
        description="Cosine similarity to the source opportunity's "
        "embedding, roughly in [0, 1] for typical text embeddings "
        "(1 = identical direction). Higher is more similar."
    )


def _truncate(text: str | None, max_chars: int = _SHORT_DESCRIPTION_MAX_CHARS) -> str | None:
    if text is None:
        return None
    if len(text) <= max_chars:
        return text
    # Truncate at the last whitespace boundary within the limit, rather
    # than cutting mid-word, then add an ellipsis.
    truncated = text[:max_chars].rsplit(" ", 1)[0]
    return f"{truncated}…"


async def get_similar_opportunities(
    db: AsyncSession, opportunity_id: UUID, limit: int = 5
) -> list[SimilarOpportunity]:
    """Find opportunities semantically similar to `opportunity_id`, using
    the stored embedding on that opportunity's own row rather than
    re-embedding any text -- this is a "more like this" lookup, so the
    query vector is already sitting in the database from whatever
    ingestion/verification step originally embedded it (Step 1.3/2.1),
    unlike `opportunity_search.semantic_search` (Step 3.1) which embeds a
    fresh natural-language query string on every call.

    Raises `RecommendationSourceNotFoundError` if `opportunity_id`
    doesn't exist, or `RecommendationSourceNotEmbeddedError` if that row
    has no embedding yet.
    """
    source = await db.get(Opportunity, opportunity_id)
    if source is None:
        raise RecommendationSourceNotFoundError(f"Opportunity {opportunity_id} not found")
    if source.embedding is None:
        raise RecommendationSourceNotEmbeddedError(
            f"Opportunity {opportunity_id} has no stored embedding to compare against"
        )

    # Same pgvector pattern as opportunity_search.semantic_search: `<=>`
    # (cosine_distance) is 0 for identical direction, up to 2 for
    # opposite; similarity = 1 - distance is the standard conversion.
    distance = Opportunity.embedding.cosine_distance(source.embedding)
    similarity = 1 - distance

    result = await db.execute(
        select(Opportunity, similarity.label("similarity_score"))
        .where(
            Opportunity.id != opportunity_id,
            # Review of Aggregated Opportunities Before Publish addition:
            # the real pre-publish gate, not `is_verified` (see
            # `admin_opportunities.py`'s own module docstring).
            Opportunity.review_status == OpportunityReviewStatus.PUBLISHED.value,
            # F-HOLDFILTER repair (P0-A): see opportunity_search.py's
            # identical addition -- same gap, same fix, same shared
            # single-source-of-truth exclusion set.
            Opportunity.lifecycle_state.not_in(USER_FACING_EXCLUDED_LIFECYCLE_STATES),
            Opportunity.embedding.is_not(None),
        )
        .order_by(distance.asc())
        .limit(limit)
    )

    return [
        SimilarOpportunity(
            id=opportunity.id,
            title=opportunity.title,
            short_description=_truncate(opportunity.description),
            similarity_score=round(float(score), 4),
        )
        for opportunity, score in result.all()
    ]


# --- Simplified Knowledge Graph addition (Deliverable 2) ---
class RelatedOpportunity(BaseModel):
    """One real opportunity related to the queried one, via a real,
    explicit `entity_relationships` edge -- not a similarity score."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    short_description: str | None = Field(
        description="The related opportunity's description, truncated to "
        f"{_SHORT_DESCRIPTION_MAX_CHARS} characters (at a word boundary)."
    )
    relationship_type: str = Field(
        description="Why these two opportunities are related: "
        "'same_organization' (same real Source) or 'same_country'."
    )


async def get_related_opportunities(
    db: AsyncSession, opportunity_id: UUID, limit: int = 10
) -> list[RelatedOpportunity]:
    """Real, simple query over the real `entity_relationships` table
    (Deliverable 2) -- no similarity math, just the real edges this
    opportunity already has, joined to the real, currently-published
    opportunity on the other end of each edge.

    Raises `RecommendationSourceNotFoundError` if `opportunity_id`
    doesn't exist -- same real 404 contract as
    `get_similar_opportunities` above.
    """
    from app.models.entity_relationship import EntityRelationship

    source = await db.get(Opportunity, opportunity_id)
    if source is None:
        raise RecommendationSourceNotFoundError(f"Opportunity {opportunity_id} not found")

    result = await db.execute(
        select(Opportunity, EntityRelationship.relationship_type)
        .join(EntityRelationship, EntityRelationship.related_opportunity_id == Opportunity.id)
        .where(
            EntityRelationship.opportunity_id == opportunity_id,
            Opportunity.review_status == OpportunityReviewStatus.PUBLISHED.value,
            # F-HOLDFILTER repair (P0-A): same gap/fix as get_similar_opportunities above.
            Opportunity.lifecycle_state.not_in(USER_FACING_EXCLUDED_LIFECYCLE_STATES),
        )
        .order_by(EntityRelationship.created_at.asc())
        .limit(limit)
    )

    return [
        RelatedOpportunity(
            id=opportunity.id,
            title=opportunity.title,
            short_description=_truncate(opportunity.description),
            relationship_type=relationship_type,
        )
        for opportunity, relationship_type in result.all()
    ]
