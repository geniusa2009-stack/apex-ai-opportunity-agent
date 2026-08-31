import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUserOptional
from app.db.session import get_db
from app.models.enums import OpportunityReviewStatus, OpportunityType
from app.models.opportunity import Opportunity
from app.models.user import User
from app.schemas.opportunity import (
    OpportunityOut,
    OpportunitySearchRequest,
    OpportunitySearchResult,
    PaginatedResponse,
)
from app.services.opportunity_search import SemanticSearchError, semantic_search

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/opportunities", tags=["opportunities"])


def _build_eligibility_filters(user: User | None) -> list[ColumnElement[bool]]:
    """Real Age/Academic-Year/Gender Opportunity Filtering step.

    Mirrors the exact NULL-handling convention `dashboards.py`'s own
    `_build_opportunity_filters` already established for
    `age_group`/`target_age_group` and `academic_level`/
    `target_academic_levels` (confirmed by reading that function before
    writing this one, rather than inventing a second, possibly-diverging
    convention): a MISSING signal on the user's own profile means that
    filter is skipped entirely (nothing excluded on that dimension); a
    MISSING/NULL value on the opportunity's own eligibility field means
    that opportunity is unrestricted on that dimension (matches every
    profile, including one with the signal set). Both sides missing is
    the common case for nearly all existing data today (see this
    endpoint's own module-level disclosure) and correctly excludes
    nothing.

    `user is None` (the fully public, unauthenticated case) returns an
    EMPTY list -- zero filters, i.e. exactly this endpoint's pre-existing
    behavior before this step, satisfying the step's own explicit
    "don't break the public unauthenticated listing" requirement by
    construction, not by a separate code path.
    """
    if user is None:
        return []

    filters: list[ColumnElement[bool]] = []

    if user.age is not None:
        filters.append(
            or_(Opportunity.min_age.is_(None), Opportunity.min_age <= user.age)
        )
        filters.append(
            or_(Opportunity.max_age.is_(None), Opportunity.max_age >= user.age)
        )

    if user.academic_level is not None:
        filters.append(
            or_(
                Opportunity.target_academic_levels.is_(None),
                Opportunity.target_academic_levels.any(user.academic_level),
            )
        )

    if user.gender is not None:
        filters.append(
            or_(Opportunity.target_gender.is_(None), Opportunity.target_gender == user.gender)
        )

    return filters


@router.get("/", response_model=PaginatedResponse[OpportunityOut])
async def list_opportunities(
    current_user: CurrentUserOptional,
    db: AsyncSession = Depends(get_db),
    type: OpportunityType | None = Query(default=None, description="Filter by opportunity type."),
    published: bool = Query(
        default=True,
        description="Only published opportunities by default (the real "
        "pre-publish review gate -- see OpportunityReviewStatus). Set to "
        "false to see everything NOT published (pending-review and "
        "rejected together). Renamed from the pre-existing `is_verified` "
        "param -- that column now reflects only the AI verification "
        "task's own legitimacy judgment, no longer what gates visibility; "
        "the tiered GET /admin/opportunities/review-queue endpoint is the "
        "real way to see the pending-review queue specifically.",
    ),
    country: str | None = Query(default=None, description="Filter by country (exact match)."),
    eligible_only: bool = Query(
        default=True,
        description="When signed in, only return opportunities matching the "
        "caller's own age/academic-level/gender profile (a missing profile "
        "field skips that filter; a missing/unrestricted opportunity field "
        "matches everyone). Has no effect at all when signed out -- the "
        "public listing is never filtered by a profile it doesn't have. "
        "Set to false while signed in to see the full unfiltered list.",
    ),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PaginatedResponse[OpportunityOut]:
    if published:
        filters = [Opportunity.review_status == OpportunityReviewStatus.PUBLISHED.value]
    else:
        filters = [Opportunity.review_status != OpportunityReviewStatus.PUBLISHED.value]
    if type is not None:
        filters.append(Opportunity.type == type)
    if country is not None:
        filters.append(Opportunity.country == country)
    if eligible_only:
        filters.extend(_build_eligibility_filters(current_user))

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
    if opportunity is None or opportunity.review_status != OpportunityReviewStatus.PUBLISHED.value:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")
    return opportunity


@router.post("/search", response_model=list[OpportunitySearchResult])
async def search_opportunities(
    payload: OpportunitySearchRequest,
    current_user: CurrentUserOptional,
    db: AsyncSession = Depends(get_db),
) -> list[OpportunitySearchResult]:
    """Semantic search over verified opportunities using pgvector cosine
    similarity. The actual query lives in
    `app.services.opportunity_search.semantic_search` -- shared with the
    Step 3.5 chatbot's search tool, extracted so both call sites use
    identical pgvector query logic rather than two copies that can drift.

    Real Age/Academic-Year/Gender Opportunity Filtering step addition:
    same `_build_eligibility_filters` this file's own `GET /opportunities/`
    listing uses, passed through as `extra_filters` -- signed-out callers
    (`current_user is None`) get an empty filter list, i.e. this
    endpoint's exact pre-existing unfiltered behavior.
    """
    try:
        return await semantic_search(
            db,
            payload.query,
            limit=payload.limit,
            min_similarity=payload.min_similarity,
            extra_filters=_build_eligibility_filters(current_user),
        )
    except SemanticSearchError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Search is temporarily unavailable -- embedding generation failed.",
        )
