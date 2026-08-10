import logging
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.opportunity import Opportunity

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
            Opportunity.is_verified.is_(True),
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
