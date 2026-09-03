import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.services.recommendation import (
    RecommendationSourceNotEmbeddedError,
    RecommendationSourceNotFoundError,
    RelatedOpportunity,
    SimilarOpportunity,
    get_related_opportunities,
    get_similar_opportunities,
)

logger = logging.getLogger(__name__)

# No `/recommendations` prefix wrapper needed beyond what's declared here --
# `prefix="/recommendations"` plus the path param below gives exactly
# `GET /api/v1/recommendations/{opportunity_id}`, matching the literal ask.
router = APIRouter(prefix="/recommendations", tags=["recommendations"])


@router.get("/{opportunity_id}", response_model=list[SimilarOpportunity])
async def recommend_similar_opportunities(
    opportunity_id: UUID,
    limit: int = Query(default=5, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
) -> list[SimilarOpportunity]:
    """Return opportunities semantically similar to `opportunity_id`, most
    similar first.

    No auth required -- deliberately matching `GET /opportunities/{id}`
    and `POST /opportunities/search` (Step 3.1), the other two
    opportunity-browsing endpoints in this codebase, both of which are
    also public. Every other AI feature in Features 2-7 required auth
    because each makes a real (rate-limited, sometimes costed) call to
    an external LLM on the user's behalf; this endpoint only reads an
    opportunity's already-stored embedding and runs a pgvector query --
    no external API call, no per-user cost -- so it's treated as public
    browsing/discovery, the same category as the two endpoints above.
    """
    try:
        return await get_similar_opportunities(db, opportunity_id, limit=limit)
    except RecommendationSourceNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")
    except RecommendationSourceNotEmbeddedError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This opportunity has no embedding yet, so recommendations "
            "can't be computed for it.",
        )


@router.get("/{opportunity_id}/related", response_model=list[RelatedOpportunity])
async def recommend_related_opportunities(
    opportunity_id: UUID,
    limit: int = Query(default=10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
) -> list[RelatedOpportunity]:
    """Simplified Knowledge Graph addition (Deliverable 2). Real edges
    from the real `entity_relationships` table -- other opportunities
    from the same real organization/country as `opportunity_id`, most
    recently-linked first. Registered as a real, distinct path segment
    (`/related`) after the existing `/{opportunity_id}` route -- no
    routing-precedence conflict, since that route has no further path
    segment of its own.

    Public, same posture as `GET /{opportunity_id}` above: a plain
    table read, no external call, no per-user cost.
    """
    try:
        return await get_related_opportunities(db, opportunity_id, limit=limit)
    except RecommendationSourceNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")
