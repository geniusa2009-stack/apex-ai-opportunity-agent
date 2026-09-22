import datetime as dt
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.db.session import get_db
from app.models.enums import OpportunityReviewStatus, OpportunityType
from app.models.opportunity import Opportunity
from app.schemas.personalization import (
    EligibilityOut,
    MatchSignalsOut,
    RecommendationItemOut,
    RecommendationsOut,
)
from app.services.personalized_ranking import DEFAULT_EXPLORATION_RATIO, rank_candidates
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


# --- Personalized Opportunity Intelligence + Opportunity Passport step
# (Part N: Recommendation API) ---
#
# AUDITED FIRST (Part 0/N's own instruction): this router already had
# TWO endpoints, both OPPORTUNITY-to-opportunity (embedding similarity /
# knowledge-graph edges), neither USER-personalized, neither requiring
# auth. `GET /recommendations` (no further path segment) is a genuinely
# new route -- FastAPI distinguishes it from `GET /recommendations/
# {opportunity_id}` by path-segment count, so there is no routing
# collision -- extending this SAME router/prefix rather than creating a
# second, parallel `/personalized-recommendations` router, per this
# phase's own "extend, don't duplicate" instruction.
#
# Registered BEFORE the `/{opportunity_id}` route in this file so a
# literal `GET /recommendations` can never be captured by the path-param
# route (not actually order-sensitive here since the two differ in
# segment count, but registered first for readability).
@router.get("", response_model=RecommendationsOut)
async def get_personalized_recommendations(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    opportunity_type: OpportunityType | None = Query(default=None, description="Filter candidates by type."),
    country: str | None = Query(default=None, description="Filter candidates by country (exact match)."),
    limit: int = Query(default=20, ge=1, le=100),
    exploration_ratio: float = Query(
        default=DEFAULT_EXPLORATION_RATIO, ge=0.0, le=1.0,
        description="Fraction of the page reserved for eligible-but-not-top-ranked opportunities (Part L).",
    ),
) -> RecommendationsOut:
    """Real, deterministic personalized recommendations (Part H's own
    safe pipeline: INELIGIBLE and CLOSED are removed before any ranking
    signal is even computed -- see `personalized_ranking.rank_candidates`).
    Auth required -- unlike the two opportunity-to-opportunity endpoints
    below, this one reads the CALLER's own profile, so there is no
    meaningful anonymous version of it.

    Returns structured explanations only (Part N's own "do not expose
    internal chain-of-thought" rule) -- `reasons`/`missing_information`
    are plain strings built from real signal values
    (`personalized_ranking.build_explanation`/`build_missing_information`),
    never a model's raw reasoning trace.
    """
    filters = [Opportunity.review_status == OpportunityReviewStatus.PUBLISHED.value]
    if opportunity_type is not None:
        filters.append(Opportunity.type == opportunity_type)
    if country is not None:
        filters.append(Opportunity.country == country)

    # Part P: a bounded candidate pool, not the entire opportunity table
    # -- a real, disclosed scope limit (see this endpoint's own Known
    # Limitations entry in the phase report) rather than an unbounded
    # full-table scan on every request.
    candidate_result = await db.execute(select(Opportunity).where(*filters).limit(500))
    candidates = list(candidate_result.scalars().all())

    bookmark_ids = {b.opportunity_id for b in current_user.bookmarks if b.opportunity_id is not None}

    ranked = await rank_candidates(
        db,
        user=current_user,
        opportunities=candidates,
        bookmarked_opportunity_ids=bookmark_ids,
        now=dt.datetime.now(dt.timezone.utc),
        exploration_ratio=exploration_ratio,
        limit=limit,
    )

    items = [
        RecommendationItemOut(
            opportunity_id=r.opportunity.id,
            title=r.opportunity.title,
            opportunity_type=r.opportunity.type.value,
            deadline=r.opportunity.deadline,
            lifecycle_state=r.opportunity.lifecycle_state,
            eligibility=EligibilityOut(
                status=r.eligibility.status.value,
                matched_rules=[m.explanation for m in r.eligibility.matched_rules],
                failed_rules=[f.explanation for f in r.eligibility.failed_rules],
                unknown_rules=[u.explanation for u in r.eligibility.unknown_rules],
                missing_information=r.eligibility.missing_information,
            ),
            signals=MatchSignalsOut(
                interest_match=r.signals.interest_match.value,
                goal_match=r.signals.goal_match.value,
                type_match=r.signals.type_match.value,
                geography_match=r.signals.geography_match.value,
                funding_match=r.signals.funding_match.value,
                deadline_match=r.signals.deadline_match.value,
                graph_relevance=r.signals.graph_relevance.value,
            ),
            personalization_score=r.personalization_score,
            reasons=r.reasons,
            missing_information=r.missing_information,
            is_exploration=r.is_exploration,
        )
        for r in ranked
    ]

    return RecommendationsOut(
        items=items,
        exploration_ratio=exploration_ratio,
        total_eligible_candidates=len(items),
    )


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
