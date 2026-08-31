import logging

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.enums import OpportunityReviewStatus
from app.models.opportunity import Opportunity
from app.schemas.opportunity import OpportunityOut, OpportunitySearchResult
from app.services.ai import ai_gateway

logger = logging.getLogger(__name__)


class SemanticSearchError(Exception):
    """Raised when embedding generation fails. Callers translate this into
    whatever's appropriate for their context -- an HTTP 502 for the REST
    endpoint (Step 3.1), a plain string the chatbot's tool can hand back to
    the LLM to explain the failure conversationally (Step 3.5)."""


async def semantic_search(
    db: AsyncSession,
    query: str,
    limit: int = 10,
    min_similarity: float | None = None,
    extra_filters: list[ColumnElement[bool]] | None = None,
) -> list[OpportunitySearchResult]:
    """Semantic search over verified opportunities using pgvector cosine
    similarity.

    Extracted out of the `/opportunities/search` endpoint (where this
    logic originally lived, Step 3.1) so it's shared verbatim by both that
    REST endpoint and the chatbot's search tool (Step 3.5) -- the pgvector
    query construction lives in exactly one place instead of being
    copy-pasted across two call sites and risking drift between them.

    `extra_filters` -- Real Age/Academic-Year/Gender Opportunity
    Filtering step addition. Optional, defaults to `None` (no extra
    filters, i.e. this function's exact pre-existing behavior) so the
    chatbot's search tool (`app/services/chatbot.py`, which has no signed-
    in user's profile readily available in its own closure) keeps working
    completely unchanged -- only `/opportunities/search`'s own REST
    endpoint passes real eligibility filters (built the same way
    `/opportunities/`'s listing endpoint does, via
    `opportunities.py::_build_eligibility_filters`) when the caller is
    signed in.

    Note on index: there's no ANN index (ivfflat/hnsw) on
    `opportunities.embedding` yet (deliberately deferred back in Step 1.2
    pending real data volume) -- this runs an exact nearest-neighbor scan
    over all verified+embedded rows. Correct, just not yet optimized for scale.
    """
    try:
        query_embedding = await ai_gateway.generate_embedding(query)
    except Exception as exc:
        logger.exception("Embedding generation failed for search query %r", query)
        raise SemanticSearchError(str(exc)) from exc

    threshold = (
        min_similarity if min_similarity is not None else settings.search_similarity_threshold
    )

    # pgvector's `<=>` operator computes cosine DISTANCE (0 = identical
    # direction, up to 2 = opposite), not similarity. `cosine_distance()`
    # is pgvector-python's SQLAlchemy comparator for that exact operator.
    # similarity = 1 - distance is the standard conversion, used both for
    # the threshold filter and for what's returned to the caller.
    distance = Opportunity.embedding.cosine_distance(query_embedding)
    similarity = 1 - distance

    result = await db.execute(
        select(Opportunity, similarity.label("similarity_score"))
        .where(
            # Review of Aggregated Opportunities Before Publish addition:
            # the real pre-publish gate, not `is_verified` (which now
            # only reflects the AI verification task's own legitimacy
            # judgment -- see `admin_opportunities.py`'s own module
            # docstring for the full reasoning).
            Opportunity.review_status == OpportunityReviewStatus.PUBLISHED.value,
            Opportunity.embedding.is_not(None),
            similarity >= threshold,
            *(extra_filters or []),
        )
        .order_by(distance.asc())
        .limit(limit)
    )

    return [
        OpportunitySearchResult(
            **OpportunityOut.model_validate(opportunity).model_dump(),
            similarity_score=round(float(score), 4),
        )
        for opportunity, score in result.all()
    ]
