import logging
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.opportunity import Opportunity

logger = logging.getLogger(__name__)


class DuplicateMatch(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    similarity_score: float = Field(
        description="Cosine similarity to the checked embedding, roughly "
        "in [0, 1] for typical text embeddings (1 = identical direction)."
    )


async def find_duplicates(
    db_session: AsyncSession,
    new_opportunity_embedding: list[float],
    similarity_threshold: float = 0.95,
    *,
    exclude_opportunity_id: UUID | None = None,
    limit: int = 10,
) -> list[DuplicateMatch]:
    """Find existing opportunities whose stored embedding is at least
    `similarity_threshold` similar (cosine) to `new_opportunity_embedding`.

    Two keyword-only parameters beyond the originally specified signature,
    both needed for this to actually work correctly rather than just
    compile:

    - `exclude_opportunity_id`: when checking an *existing* opportunity
      for duplicates against the rest of the table (the router's
      `opportunity_id` path), that row's own embedding is identical to
      itself (similarity 1.0) and would otherwise always show up as its
      own "duplicate." Without this, `POST /check-duplicate` would
      report every existing opportunity as a duplicate of itself.
    - `limit`: an unbounded query here could return every row above the
      threshold on a large, genuinely duplicate-heavy table; bounding it
      keeps this a manageable "candidates to review" list rather than
      every match in the database, same rationale as every other listy
      endpoint in this codebase (`semantic_search`, recommendations).

    Results are ordered most-similar-first.
    """
    # Same pgvector pattern as opportunity_search.py / recommendation.py:
    # `<=>` (cosine_distance) is 0 for identical direction, up to 2 for
    # opposite; similarity = 1 - distance is the standard conversion.
    distance = Opportunity.embedding.cosine_distance(new_opportunity_embedding)
    similarity = 1 - distance

    filters = [
        Opportunity.embedding.is_not(None),
        similarity >= similarity_threshold,
    ]
    if exclude_opportunity_id is not None:
        filters.append(Opportunity.id != exclude_opportunity_id)

    result = await db_session.execute(
        select(Opportunity, similarity.label("similarity_score"))
        .where(*filters)
        .order_by(distance.asc())
        .limit(limit)
    )

    return [
        DuplicateMatch(id=opportunity.id, title=opportunity.title, similarity_score=round(float(score), 4))
        for opportunity, score in result.all()
    ]
