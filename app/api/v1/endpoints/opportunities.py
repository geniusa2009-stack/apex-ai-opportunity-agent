import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.enums import OpportunityType
from app.models.opportunity import Opportunity
from app.schemas.opportunity import (
    OpportunityOut,
    OpportunitySearchRequest,
    OpportunitySearchResult,
    PaginatedResponse,
)
from app.services.opportunity_search import SemanticSearchError, semantic_search

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/opportunities", tags=["opportunities"])


@router.get("/", response_model=PaginatedResponse[OpportunityOut])
async def list_opportunities(
    db: AsyncSession = Depends(get_db),
    type: OpportunityType | None = Query(default=None, description="Filter by opportunity type."),
    is_verified: bool = Query(
        default=True,
        description="Only verified opportunities by default. Set to false to "
        "see the review queue (flagged/unprocessed rows) instead.",
    ),
    country: str | None = Query(default=None, description="Filter by country (exact match)."),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PaginatedResponse[OpportunityOut]:
    filters = [Opportunity.is_verified.is_(is_verified)]
    if type is not None:
        filters.append(Opportunity.type == type)
    if country is not None:
        filters.append(Opportunity.country == country)

    total = (
        await db.execute(select(func.count()).select_from(Opportunity).where(*filters))
    ).scalar_one()

    result = await db.execute(
        select(Opportunity)
        .where(*filters)
        .order_by(Opportunity.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    opportunities = result.scalars().all()

    return PaginatedResponse[OpportunityOut](
        items=opportunities, total=total, limit=limit, offset=offset
    )


@router.get("/{opportunity_id}", response_model=OpportunityOut)
async def get_opportunity(
    opportunity_id: UUID, db: AsyncSession = Depends(get_db)
) -> Opportunity:
    opportunity = await db.get(Opportunity, opportunity_id)
    if opportunity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")
    return opportunity


@router.post("/search", response_model=list[OpportunitySearchResult])
async def search_opportunities(
    payload: OpportunitySearchRequest, db: AsyncSession = Depends(get_db)
) -> list[OpportunitySearchResult]:
    """Semantic search over verified opportunities using pgvector cosine
    similarity. The actual query lives in
    `app.services.opportunity_search.semantic_search` -- shared with the
    Step 3.5 chatbot's search tool, extracted so both call sites use
    identical pgvector query logic rather than two copies that can drift.
    """
    try:
        return await semantic_search(
            db, payload.query, limit=payload.limit, min_similarity=payload.min_similarity
        )
    except SemanticSearchError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Search is temporarily unavailable -- embedding generation failed.",
        )
