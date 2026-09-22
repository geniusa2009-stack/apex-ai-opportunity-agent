import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, CurrentUserOptional
from app.db.session import get_db
from app.models.bookmark import UserBookmark
from app.models.enums import BookmarkStatus, InteractionEventType, OpportunityReviewStatus, OpportunityType
from app.models.opportunity import Opportunity
from app.models.user import User
from app.schemas.opportunity import (
    OpportunityOut,
    OpportunitySearchRequest,
    OpportunitySearchResult,
    PaginatedResponse,
)
from app.schemas.personalization import (
    EligibilityOut,
    OpportunityPassportOut,
    PassportFundingOut,
    PassportIdentityOut,
    PassportProvenanceOut,
    PassportTemporalOut,
    PassportTrustOut,
)
from app.schemas.user_intelligence import OpportunityFeedbackIn, OpportunityFeedbackOut
from app.services import interaction_service
from app.services.opportunity_passport import build_passport
from app.services.opportunity_search import SemanticSearchError, semantic_search
from sqlalchemy.exc import IntegrityError

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
    opportunity_id: UUID,
    current_user: CurrentUserOptional,
    db: AsyncSession = Depends(get_db),
) -> Opportunity:
    opportunity = await db.get(Opportunity, opportunity_id)
    if opportunity is None or opportunity.review_status != OpportunityReviewStatus.PUBLISHED.value:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")

    # APEX User Intelligence + Proactive Guidance step, Part D: a real
    # VIEWED event, recorded ONLY for an authenticated caller (never
    # attributed to an anonymous request -- there is no `user_id` to
    # attach it to). Best-effort: this route's own contract is "return
    # the opportunity", not "record analytics" -- a failure to record the
    # interaction must never turn a successful detail-page load into an
    # error for the caller.
    if current_user is not None:
        try:
            await interaction_service.record_interaction(
                db,
                user_id=current_user.id,
                opportunity_id=opportunity_id,
                event_type=InteractionEventType.VIEWED,
            )
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("Failed to record VIEWED interaction for opportunity %s", opportunity_id)

    return opportunity


# --- Personalized Opportunity Intelligence + Opportunity Passport step
# (Part O: Opportunity Passport API) ---
#
# AUDITED FIRST: no equivalent endpoint existed (confirmed via a full
# read of this router and `recommendations.py` before writing this --
# neither the opportunity-to-opportunity similarity/related endpoints
# nor `GET /{opportunity_id}` itself return anything like the Passport's
# trust/temporal/provenance/eligibility projection), so this is a real,
# new, additive route rather than a duplicate.
#
# `CurrentUserOptional` -- same posture as `GET /{opportunity_id}` and
# `POST /search` above: publicly viewable (same 404-if-not-published
# gate), but `eligibility` in the response is personalized ONLY for a
# real authenticated caller (`build_passport`'s own `requesting_user`
# parameter) -- never accepts a `user_id` from the request itself, so
# there is no IDOR surface here: nobody can ask for anyone else's
# eligibility, only their own, via their own auth token.
@router.get("/{opportunity_id}/passport", response_model=OpportunityPassportOut)
async def get_opportunity_passport(
    opportunity_id: UUID,
    current_user: CurrentUserOptional,
    db: AsyncSession = Depends(get_db),
) -> OpportunityPassportOut:
    opportunity = await db.get(Opportunity, opportunity_id)
    if opportunity is None or opportunity.review_status != OpportunityReviewStatus.PUBLISHED.value:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")

    passport = await build_passport(db, opportunity=opportunity, requesting_user=current_user)

    eligibility_out = None
    if passport.eligibility is not None:
        eligibility_out = EligibilityOut(
            status=passport.eligibility.status,
            matched_rules=passport.eligibility.matched_rules,
            failed_rules=passport.eligibility.failed_rules,
            unknown_rules=passport.eligibility.unknown_rules,
            missing_information=passport.eligibility.missing_information,
        )

    return OpportunityPassportOut(
        identity=PassportIdentityOut(
            id=passport.identity.id,
            title=passport.identity.title,
            provider_source_id=passport.identity.provider_source_id,
            canonical_url=passport.identity.canonical_url,
            opportunity_type=passport.identity.opportunity_type,
            raw_opportunity_type_label=passport.identity.raw_opportunity_type_label,
        ),
        eligibility=eligibility_out,
        funding=PassportFundingOut(funding_details=passport.funding.funding_details),
        temporal=PassportTemporalOut(
            lifecycle_state=passport.temporal.lifecycle_state,
            lifecycle_state_reason=passport.temporal.lifecycle_state_reason,
            deadline=passport.temporal.deadline,
            last_verified=passport.temporal.last_verified,
            last_updated=passport.temporal.last_updated,
            recent_changes=passport.temporal.recent_changes,
        ),
        trust=PassportTrustOut(
            verification_status=passport.trust.verification_status,
            verification_reason=passport.trust.verification_reason,
            evidence_count=passport.trust.evidence_count,
            source_count=passport.trust.source_count,
            authoritative_source_count=passport.trust.authoritative_source_count,
            contradiction_count=passport.trust.contradiction_count,
            last_evidence_update=passport.trust.last_evidence_update,
        ),
        provenance=PassportProvenanceOut(
            source_ids=passport.provenance.source_ids,
            evidence_ids=passport.provenance.evidence_ids,
            claim_count=passport.provenance.claim_count,
        ),
    )


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


# APEX User Intelligence + Proactive Guidance step, Part O: the feedback
# loop. Four literal feedback phrases -- a closed vocabulary, never free
# text (`OpportunityFeedbackIn.feedback_type` is validated against this
# exact set below, a 422 for anything else). Registered on THIS router
# (opportunities), not `user_intelligence.py`, because the resource this
# feedback is about is the opportunity, matching this file's own existing
# `/opportunities/{opportunity_id}/passport` precedent rather than
# inventing a new `/users/me/feedback/{opportunity_id}` shape.
_FEEDBACK_EVENT_TYPES: dict[str, InteractionEventType] = {
    "not_interested": InteractionEventType.DISMISSED,
    "not_relevant": InteractionEventType.MARKED_NOT_RELEVANT,
    "more_like_this": InteractionEventType.MORE_LIKE_THIS,
}


@router.post("/{opportunity_id}/feedback", response_model=OpportunityFeedbackOut)
async def submit_opportunity_feedback(
    opportunity_id: UUID,
    payload: OpportunityFeedbackIn,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> OpportunityFeedbackOut:
    """Part O's four feedback phrases:

    - "Not interested"    -> DISMISSED (hard-excluded from future feeds,
                              see `interaction_service.
                              get_dismissed_opportunity_ids`)
    - "Not relevant"      -> MARKED_NOT_RELEVANT (same hard-exclusion)
    - "Already applied"   -> NOT a new `OpportunityInteraction` row --
                              this is exactly what `UserBookmark.status ==
                              BookmarkStatus.APPLIED` already means
                              (Part 0's own audit finding: don't duplicate
                              an existing capability), so this branch
                              creates the bookmark if missing or updates
                              its status if present, rather than adding a
                              second, parallel "applied" signal.
    - "Show me more like this" -> MORE_LIKE_THIS (explicit positive
                              intent, a real, documented, positive
                              behavioral-weight signal -- see
                              `interaction_service.BEHAVIOR_SIGNAL_WEIGHT`)

    Auth required (`CurrentUser`, not `CurrentUserOptional`): feedback is
    inherently a per-user signal: there is no meaningful anonymous
    feedback event to record.
    """
    opportunity = await db.get(Opportunity, opportunity_id)
    if opportunity is None or opportunity.review_status != OpportunityReviewStatus.PUBLISHED.value:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")

    if payload.feedback_type == "already_applied":
        existing = await db.execute(
            select(UserBookmark).where(
                UserBookmark.user_id == current_user.id,
                UserBookmark.opportunity_id == opportunity_id,
            )
        )
        bookmark = existing.scalar_one_or_none()
        if bookmark is None:
            bookmark = UserBookmark(
                user_id=current_user.id, opportunity_id=opportunity_id, status=BookmarkStatus.APPLIED
            )
            db.add(bookmark)
            try:
                await db.commit()
            except IntegrityError:
                # Same race precedent as `bookmarks.py::create_bookmark`.
                await db.rollback()
                existing = await db.execute(
                    select(UserBookmark).where(
                        UserBookmark.user_id == current_user.id,
                        UserBookmark.opportunity_id == opportunity_id,
                    )
                )
                bookmark = existing.scalar_one()
                bookmark.status = BookmarkStatus.APPLIED
                await db.commit()
        elif bookmark.status != BookmarkStatus.APPLIED:
            bookmark.status = BookmarkStatus.APPLIED
            await db.commit()
        return OpportunityFeedbackOut(feedback_type=payload.feedback_type, recorded=True)

    event_type = _FEEDBACK_EVENT_TYPES.get(payload.feedback_type)
    if event_type is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "feedback_type must be one of: not_interested, not_relevant, "
                "already_applied, more_like_this."
            ),
        )

    await interaction_service.record_interaction(
        db, user_id=current_user.id, opportunity_id=opportunity_id, event_type=event_type
    )
    await db.commit()

    return OpportunityFeedbackOut(feedback_type=payload.feedback_type, recorded=True)
