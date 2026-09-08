"""Review of Aggregated Opportunities Before Publish.

STEP 1 INVESTIGATION (re-read `app/services/ai.py`'s verification
pipeline, `app/workers/tasks.py::verify_new_opportunities`, the full
`Opportunity` model, and `app/api/v1/endpoints/opportunities.py`'s real
listing/search endpoints before writing anything here): there was no
real human pre-publish gate anywhere in this codebase. `Opportunity.
is_verified` was flipped straight to `True` by
`_verify_new_opportunities` alone, the moment its own AI confidence
score cleared `settings.ai_confidence_threshold` -- no review queue, no
approve/reject action, no `ModerationLogEntry` write, nothing. A
"legitimate but not yet human-reviewed" opportunity went fully live the
instant the AI verification task happened to run. This is a genuinely
new gate, not an existing one this step merely exposes.

STEP 2 DESIGN: `OpportunityReviewStatus` (app/models/enums.py) is the new
named lifecycle enum -- `PENDING_REVIEW` (the real default for every new
row) / `PUBLISHED` / `REJECTED`. `Opportunity.is_verified` is left
completely alone and keeps its own pre-existing, narrower meaning (the
AI verification task's own legitimacy judgment) -- it is now purely
INFORMATIONAL to the human reviewer below, not what gates visibility.
`review_status` is the one real thing that does (see
`Opportunity.review_status`'s own column docstring, and the four real
browsing-surface query sites migration 0044 + this step update:
`opportunities.py::list_opportunities`/`get_opportunity`,
`opportunity_search.py::semantic_search`, `recommendation.py`'s
"similar opportunities" query). `verify_new_opportunities` itself needed
ZERO code changes -- it still runs exactly as before, still writes
`is_verified`/`ai_confidence_score`/the cleaned title/deadline/etc.
fields, all of which now serve as real, useful signal a human reviewer
sees on this queue instead of driving visibility on their own.

STEP 3.3, CLAIM SYSTEM REUSE DECISION (real fit, not a default): YES.
`admin_claims.py`'s own `_CLAIMABLE_MODELS` extension point exists
precisely so a genuinely new claimable queue is one dict entry, not a
new pair of endpoints -- confirmed empirically twice already
(`ChatMessageReport`, `ModerationAppeal`, neither needed new claim
routes). A pending-review `Opportunity` is exactly the same shape of
problem the video/chat-report queues solved claiming for: multiple
Moderators independently opening the SAME queue and potentially
redundantly researching/deciding on the same aggregated listing. Two
new plain columns (`claimed_by`/`claimed_at`, migration 0044) satisfy
`moderation_claim_service._Claimable`'s structural Protocol the exact
same way `StudentVideo`/`ChatMessageReport`/`ModerationAppeal` already
do -- `POST/DELETE /admin/moderation/opportunity/{id}/claim` work
automatically via the existing generic claim router with zero new
endpoints, once `ModerationTargetType.OPPORTUNITY` is added to that
dispatch table (see this file's own router registration note below).
Same Step 3 posture as every other claimable queue: claiming is
advisory-only (visibility, not a hard lock) -- approve/reject below do
NOT check or require a claim.

DISCLOSED SCOPE DECISION: no notification is sent on approve/reject,
unlike `override_video_decision`'s reject-notifies-the-student branch.
Confirmed via a full read of `Opportunity` before writing this: an
aggregated/scraped opportunity has no "owner" user at all (it comes from
a `Source`, not a submitting student) -- there is no one to notify.
`notes` on the reject action is purely an audit-trail note stored in the
`ModerationLogEntry.outcome`, not user-facing copy.

TIER DECISION: `RequireModerationAccess` (Moderator-or-higher) for all
three routes below -- approving/rejecting a pending opportunity is a
real, reviewable, REVERSIBLE decision (a Moderator or Owner can always
approve/reject again later to flip it), the same tier
`override_video_decision` already uses for the identical shape of
decision on `StudentVideo`. Not `RequireSeniorModerationAccess` --
nothing here is irreversibly destructive (no storage/row deletion, no
Redis-backed unrecoverable ban), so the Senior-Moderator-only
"destructive action" tier this codebase reserves for
`delete_video_permanently`/`reject_video_and_purge_media` does not
apply.

=============================================================================
Manual Opportunity Creation, with Designated-Team-Member Access (no new table/column on `opportunities`/`sources` themselves; migration 0050 adds the real `ALTER TYPE moderation_action ADD VALUE` the new OPPORTUNITY_MANUAL_CREATE member needs -- see that migration's own SELF-CORRECTION docstring)
=============================================================================

STEP 1 INVESTIGATION: confirmed via a full read of `OpportunityCreate`
(app/schemas/opportunity.py) and `aggregator_service.upsert_opportunity`
that no manual, non-scraper/aggregator creation path existed anywhere --
every real `Opportunity` row before this addition was either
Scrapy-pipeline- or aggregator-pipeline-authored. A genuine gap: a
Moderator who hears about a real scholarship directly (word of mouth, a
personal email, a partner org) had no way to add it -- it would simply
never appear unless some future scrape happened to pick it up.

STEP 2 DESIGN, ACCESS TIER: the prompt's own framing -- give this to
"themselves (Owner) and specific team members they personally
designate" -- is exactly what the existing 3-tier `AdminRole` system
already provides: an Owner promotes a trusted volunteer to
`SENIOR_MODERATOR` via the existing Owner-only
`PATCH /admin/users/{user_id}/role`, and that IS the "personally
designate" act the prompt describes. Gating this route at
`RequireSeniorModerationAccess` reuses that existing, already-audited
control rather than inventing a new, separate `can_create_opportunities`-
style boolean that would duplicate a decision the Owner already makes
through role assignment -- a second permission dimension with no
independent real-world meaning here. Deliberately NOT
`RequireModerationAccess` (would auto-grant this to every Moderator,
which the prompt explicitly said NOT to do) and NOT `RequireOwner`
(would exclude every "specific team member" the prompt explicitly says
should be reachable).

STEP 2 DESIGN, REVIEW STATUS: a manually created row is written with
`review_status=PUBLISHED` directly, not `PENDING_REVIEW`. The
`PENDING_REVIEW` gate (this file's own module docstring above) exists
specifically to catch bad AI-scraped/extracted content before a human
ever sees it -- a Senior-Moderator-or-higher typing the row by hand,
having personally verified the opportunity is real, already performed
that exact human check; routing it through the same queue a second time
would be redundant process, not an added safety margin. `is_verified`
is left `False` -- honest: this row never went through the separate AI
verification pipeline (`verify_new_opportunities`), and that field's own
meaning (see this file's Step 2 note above) is specifically "the AI
task's own legitimacy judgment," which never ran here.

STEP 2 DESIGN, SOURCE ROW: every `Opportunity.source_id` is a real
`NOT NULL` FK (app/models/opportunity.py) -- a manually created row
still needs one. Reuses `aggregator_service.get_or_create_source`
unmodified (not re-implemented) against a single shared, well-known
`Source` row (`name="Manual Entry"`), so every hand-typed opportunity
correctly attributes to "a human typed this," distinguishable from any
scraped/aggregated `Source` in every existing read (`OpportunityOut.
source.source_type == SourceType.MANUAL`, a new, honest fourth enum
value -- see `SourceType`'s own docstring in app/models/enums.py for why
reusing `WEBSITE` would have been a misrepresentation).

STEP 3.3, MODERATION LOG: a new `ModerationAction.OPPORTUNITY_MANUAL_CREATE`
member (app/models/enums.py) -- same direct-target,
`target_type=OPPORTUNITY` convention `OPPORTUNITY_APPROVE`/
`OPPORTUNITY_REJECT` already use.
"""

import logging
import uuid

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from app.api.deps import (
    CurrentUser,
    DbSession,
    RequireModerationAccess,
    RequireSeniorModerationAccess,
    effective_admin_role,
)
from app.models.claim import Claim
from app.models.contradiction import Contradiction
from app.models.enums import (
    AcademicLevel,
    EvidenceStatus,
    ModerationAction,
    ModerationTargetType,
    OpportunityReviewStatus,
    OpportunityType,
    SourceType,
    VerificationDecisionStatus,
)
from app.models.evidence import Evidence
from app.models.opportunity import Opportunity
from app.models.user import User
from app.schemas.evidence import (
    ClaimOut,
    ContradictionOut,
    EvaluatedClaimOut,
    EvidenceOut,
    EvidenceStatsOut,
    VerificationDecisionOut,
)
from app.schemas.opportunity import (
    AdminOpportunityCreate,
    AdminOpportunityDecisionRequest,
    AdminOpportunityDecisionResponse,
    AdminOpportunityReviewOut,
    CoverageBucket,
    OpportunityCoverageOut,
    OpportunityOut,
    PaginatedResponse,
)
from app.services import evidence_verification, moderation_claim_service, moderation_log_service
from app.services.aggregator_service import get_or_create_source

# The one shared `Source` row every manually created `Opportunity`
# attributes to -- see this file's own module docstring, "Manual
# Opportunity Creation" section, "STEP 2 DESIGN, SOURCE ROW".
_MANUAL_SOURCE_NAME = "Manual Entry"
_MANUAL_SOURCE_BASE_URL = "internal://manual-entry"

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-opportunities"])


def _build_review_item(opportunity: Opportunity, claimant_name: str | None) -> AdminOpportunityReviewOut:
    """`OpportunityOut.model_validate(opportunity)` first, via the same
    `from_attributes=True` conversion every other real read of this
    model already goes through (correctly converts the eager-loaded
    `source` relationship into a nested `SourceOut`) -- then layered with
    the Claim System's own resolved, non-expired claim state, same
    `get_effective_claim` call every other claimable queue's listing
    already makes (an expired claim renders as unclaimed here too)."""
    base = OpportunityOut.model_validate(opportunity)
    effective_claimed_by, effective_claimed_at = moderation_claim_service.get_effective_claim(
        opportunity
    )
    return AdminOpportunityReviewOut(
        **base.model_dump(),
        claimed_by=effective_claimed_by,
        claimed_by_name=claimant_name if effective_claimed_by is not None else None,
        claimed_at=effective_claimed_at,
    )


@router.post(
    "/admin/opportunities",
    response_model=OpportunityOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_opportunity_manually(
    payload: AdminOpportunityCreate,
    current_user: CurrentUser,
    db: DbSession,
    _: RequireSeniorModerationAccess,
) -> OpportunityOut:
    """Senior-Moderator-or-higher. See this file's own module docstring,
    "Manual Opportunity Creation" section, for the full access-tier /
    review-status / source-row reasoning. Written straight to
    `review_status=PUBLISHED`, `is_verified=False`.
    """
    source = await get_or_create_source(
        db,
        name=_MANUAL_SOURCE_NAME,
        base_url=_MANUAL_SOURCE_BASE_URL,
        source_type=SourceType.MANUAL,
    )

    opportunity = Opportunity(
        title=payload.title,
        description=payload.description,
        target_url=payload.target_url,
        source_id=source.id,
        type=payload.opportunity_type,
        deadline=payload.deadline,
        country=payload.country,
        is_local_egypt=payload.is_local_egypt,
        target_age_group=payload.target_age_group,
        target_academic_levels=payload.target_academic_levels,
        min_age=payload.min_age,
        max_age=payload.max_age,
        target_gender=payload.target_gender,
        review_status=OpportunityReviewStatus.PUBLISHED.value,
        is_verified=False,
        ai_confidence_score=None,
        raw_extracted_data=None,
        embedding=None,
    )
    db.add(opportunity)
    await db.flush()

    moderation_log_service.log_action(
        db,
        actor_id=current_user.id,
        actor_role=effective_admin_role(current_user),
        action=ModerationAction.OPPORTUNITY_MANUAL_CREATE,
        target_type=ModerationTargetType.OPPORTUNITY,
        target_id=opportunity.id,
        outcome={"title": opportunity.title},
    )

    await db.commit()
    await db.refresh(opportunity)

    return OpportunityOut.model_validate(opportunity)


@router.get(
    "/admin/opportunities/review-queue",
    response_model=PaginatedResponse[AdminOpportunityReviewOut],
)
async def get_opportunity_review_queue(
    db: DbSession,
    current_user: CurrentUser,
    _: RequireModerationAccess,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PaginatedResponse[AdminOpportunityReviewOut]:
    """Moderator-or-higher. Every `Opportunity` currently
    `PENDING_REVIEW`, oldest first (`ORDER BY created_at ASC`) -- same
    "the longest-waiting item surfaces first" posture
    `get_review_queue` (videos) already established, so nothing sits
    unreviewed indefinitely just because newer scrapes keep landing on
    top of it. Includes rows regardless of whether the AI verification
    task has processed them yet (`is_verified`/`ai_confidence_score` may
    still be `None`/unset) -- a human can review an opportunity before
    or independently of the AI pipeline; that pipeline's own output is
    shown here as informational signal, not a queue-membership filter.
    """
    filters = (Opportunity.review_status == OpportunityReviewStatus.PENDING_REVIEW.value,)

    total = (
        await db.execute(select(func.count()).select_from(Opportunity).where(*filters))
    ).scalar_one()

    Claimant = aliased(User)
    result = await db.execute(
        select(Opportunity, Claimant.full_name)
        .outerjoin(Claimant, Claimant.id == Opportunity.claimed_by)
        .where(*filters)
        .order_by(Opportunity.created_at.asc())
        .limit(limit)
        .offset(offset)
    )
    rows = result.all()

    items = [_build_review_item(opportunity, claimant_name) for opportunity, claimant_name in rows]

    return PaginatedResponse[AdminOpportunityReviewOut](
        items=items, total=total, limit=limit, offset=offset
    )


async def _get_opportunity_or_404(db: DbSession, opportunity_id: uuid.UUID) -> Opportunity:
    opportunity = await db.get(Opportunity, opportunity_id)
    if opportunity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")
    return opportunity


@router.post(
    "/admin/opportunities/{opportunity_id}/approve",
    response_model=AdminOpportunityDecisionResponse,
)
async def approve_opportunity(
    opportunity_id: uuid.UUID,
    payload: AdminOpportunityDecisionRequest,
    current_user: CurrentUser,
    db: DbSession,
    _: RequireModerationAccess,
) -> AdminOpportunityDecisionResponse:
    """Moderator-or-higher. Sets `review_status = PUBLISHED` -- the one
    real action that makes a pending, aggregated/scraped opportunity
    visible to students (see this router's own module docstring for the
    four real browsing-surface query sites that now gate on this
    column). No restriction on the opportunity's CURRENT `review_status`
    -- same generosity `override_video_decision` already extends: a
    Moderator can re-approve an already-published row (e.g. re-affirming
    after a later look), or reverse an earlier rejection. Not gated by
    claim state at all (see this router's own module docstring's Step 3
    reasoning) -- any Moderator-or-higher may act regardless of who, if
    anyone, currently holds the claim.
    """
    opportunity = await _get_opportunity_or_404(db, opportunity_id)

    opportunity.review_status = OpportunityReviewStatus.PUBLISHED.value
    db.add(opportunity)

    moderation_log_service.log_action(
        db,
        actor_id=current_user.id,
        actor_role=effective_admin_role(current_user),
        action=ModerationAction.OPPORTUNITY_APPROVE,
        target_type=ModerationTargetType.OPPORTUNITY,
        target_id=opportunity.id,
        outcome={"notes": payload.notes},
    )

    await db.commit()
    await db.refresh(opportunity)

    return AdminOpportunityDecisionResponse(
        success=True,
        opportunity_id=opportunity.id,
        new_status=OpportunityReviewStatus(opportunity.review_status),
    )


@router.post(
    "/admin/opportunities/{opportunity_id}/reject",
    response_model=AdminOpportunityDecisionResponse,
)
async def reject_opportunity(
    opportunity_id: uuid.UUID,
    payload: AdminOpportunityDecisionRequest,
    current_user: CurrentUser,
    db: DbSession,
    _: RequireModerationAccess,
) -> AdminOpportunityDecisionResponse:
    """Moderator-or-higher. Sets `review_status = REJECTED` -- the row
    itself is NOT deleted (same "keep the audit trail, don't destroy
    data" posture the video queue's own `override_video_decision`
    reject branch already established for `raw_extracted_data`): a
    rejected opportunity stays in the database, permanently excluded
    from every real browsing-surface query, reachable again later via
    the review queue's own history or a future re-approve call if the
    decision needs reversing. No notification is sent -- see this
    router's own module docstring for why an aggregated `Opportunity`
    has no submitting-user "owner" to notify, unlike a rejected
    `StudentVideo`.
    """
    opportunity = await _get_opportunity_or_404(db, opportunity_id)

    opportunity.review_status = OpportunityReviewStatus.REJECTED.value
    db.add(opportunity)

    moderation_log_service.log_action(
        db,
        actor_id=current_user.id,
        actor_role=effective_admin_role(current_user),
        action=ModerationAction.OPPORTUNITY_REJECT,
        target_type=ModerationTargetType.OPPORTUNITY,
        target_id=opportunity.id,
        outcome={"notes": payload.notes},
    )

    await db.commit()
    await db.refresh(opportunity)

    return AdminOpportunityDecisionResponse(
        success=True,
        opportunity_id=opportunity.id,
        new_status=OpportunityReviewStatus(opportunity.review_status),
    )


# --- Search Coverage / Gap Analysis addition (Deliverable 1) ---
_GAP_THRESHOLD_RATIO = 0.1


def _build_buckets(counts: dict[str, int], *, all_known_values: list[str] | None) -> list[CoverageBucket]:
    """Real counts -> real, explainable gap buckets.

    `all_known_values`, when given (the two fixed-vocabulary dimensions,
    `type`/`target_academic_levels`), forces every real enum value to
    appear even at a real count of zero -- a zero IS the finding, not a
    missing row this function should silently drop. For the open-
    vocabulary `country` dimension (`all_known_values=None`), only
    countries that genuinely appear in the data are listed.

    A value is flagged `is_gap=True` when its own real count is below
    `_GAP_THRESHOLD_RATIO` (10%) of the single highest real count in
    this same dimension -- a plain, real comparison against real data,
    not a fabricated weighted "coverage score". When every real count in
    a dimension is zero (an empty table), nothing is flagged as a gap
    relative to another zero.
    """
    values = list(all_known_values) if all_known_values is not None else sorted(
        counts, key=lambda v: counts[v], reverse=True
    )
    max_count = max(counts.values(), default=0)
    threshold = max_count * _GAP_THRESHOLD_RATIO
    buckets = []
    for value in values:
        count = counts.get(value, 0)
        is_gap = max_count > 0 and count <= threshold
        buckets.append(CoverageBucket(value=value, count=count, is_gap=is_gap))
    if all_known_values is not None:
        buckets.sort(key=lambda b: b.count, reverse=True)
    return buckets


async def compute_opportunity_coverage(db: DbSession) -> OpportunityCoverageOut:
    """Real, simple gap analysis over the real `Opportunity` table --
    real counts by `type`/`country`/`target_academic_levels`, with real,
    disclosed comparisons (see `_build_buckets`) flagging under-covered
    values. No AI call, no learned score: this is a real `SELECT` plus
    real Python counting.

    Deliverable 4 addition: pulled out of the endpoint body below (which
    now just calls this) so `app/services/research_orchestrator.py` can
    reuse the exact same real coverage computation as a routing-depth
    SIGNAL (poor coverage -> deeper discovery justified) without a second,
    duplicated query -- see that module's own "D1 Coverage Integration"
    section. Coverage counts stay a planning signal only there, never
    promoted to an opportunity-truth field, same as this endpoint's own
    pre-existing posture.

    STEP 1 INVESTIGATION note: fetched with plain column selects (not
    `Opportunity.target_academic_levels.any(...)`, which only compiles
    against the real Postgres `ARRAY` type) so this same query runs
    correctly against both the real Postgres deployment and this
    project's established SQLite test/live-verify substitute.
    """
    result = await db.execute(
        select(
            Opportunity.type,
            Opportunity.country,
            Opportunity.target_academic_levels,
        )
    )
    rows = result.all()

    total = len(rows)
    type_counts: dict[str, int] = {}
    country_counts: dict[str, int] = {}
    level_counts: dict[str, int] = {}

    for opp_type, country, academic_levels in rows:
        type_value = opp_type.value if hasattr(opp_type, "value") else str(opp_type)
        type_counts[type_value] = type_counts.get(type_value, 0) + 1

        country_value = country or "Unspecified"
        country_counts[country_value] = country_counts.get(country_value, 0) + 1

        for level in academic_levels or []:
            level_value = level.value if hasattr(level, "value") else str(level)
            level_counts[level_value] = level_counts.get(level_value, 0) + 1

    return OpportunityCoverageOut(
        total_opportunities=total,
        by_type=_build_buckets(type_counts, all_known_values=[t.value for t in OpportunityType]),
        by_country=_build_buckets(country_counts, all_known_values=None),
        by_academic_level=_build_buckets(
            level_counts, all_known_values=[lvl.value for lvl in AcademicLevel]
        ),
        gap_threshold_ratio=_GAP_THRESHOLD_RATIO,
    )


@router.get(
    "/admin/opportunities/coverage",
    response_model=OpportunityCoverageOut,
)
async def get_opportunity_coverage(
    db: DbSession,
    _: RequireModerationAccess,
) -> OpportunityCoverageOut:
    """Moderator-or-higher. See `compute_opportunity_coverage` for the
    real computation this endpoint just exposes over HTTP."""
    return await compute_opportunity_coverage(db)


# ---------------------------------------------------------------------
# D6 (Evidence & Verification Intelligence) admin read endpoints --
# completing D6's own originally-scoped, then-deferred admin surface.
# Additive observability only: read-only, `RequireModerationAccess`
# (same tier as `get_opportunity_coverage`/`get_opportunity_review_queue`
# above -- internal research/verification tooling, not public browsing,
# but no need for Senior-Moderator-only either). None of these routes
# write anything; the core evidence/claim/verification decision logic
# itself lives entirely in `app/services/evidence_verification.py` and
# is untouched here.
#
# Registered on THIS router (not `admin_research.py`) because every one
# of these is opportunity-scoped under the already-established
# `/admin/opportunities/{opportunity_id}/...` path prefix this file's
# own `review-queue`/`coverage`/claim-decision routes already use --
# consistent with the URL, not with which deliverable number introduced
# the underlying data.
# ---------------------------------------------------------------------


@router.get(
    "/admin/opportunities/{opportunity_id}/evidence",
    response_model=list[EvidenceOut],
)
async def get_opportunity_evidence(
    opportunity_id: uuid.UUID,
    db: DbSession,
    _: RequireModerationAccess,
    claim_id: uuid.UUID | None = Query(default=None),
) -> list[EvidenceOut]:
    """Moderator-or-higher. All real, persisted `Evidence` rows for one
    opportunity (optionally narrowed to one `claim_id`), most recent
    first. 404 when the opportunity itself doesn't exist; a real
    opportunity with no evidence yet (e.g. a legacy pre-D6 row) cleanly
    returns `[]`, never a 500 -- see `Evidence`'s own module docstring
    for why this is the real backward-compatibility shape. Never
    exposes `evidence_metadata` -- see `app/schemas/evidence.py`'s own
    module docstring."""
    opportunity = await db.get(Opportunity, opportunity_id)
    if opportunity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")

    stmt = select(Evidence).where(Evidence.opportunity_id == opportunity_id)
    if claim_id is not None:
        stmt = stmt.where(Evidence.claim_id == claim_id)
    stmt = stmt.order_by(Evidence.retrieved_at.desc())
    result = await db.execute(stmt)
    rows = result.scalars().all()
    return [EvidenceOut.model_validate(row) for row in rows]


@router.get(
    "/admin/opportunities/{opportunity_id}/claims",
    response_model=list[ClaimOut],
)
async def get_opportunity_claims(
    opportunity_id: uuid.UUID,
    db: DbSession,
    _: RequireModerationAccess,
) -> list[ClaimOut]:
    """Moderator-or-higher. All real, persisted `Claim` rows for one
    opportunity, each with a real `evidence_count` (a genuine
    `COUNT(*)` against the `evidence` table, via an outer join+group-by
    -- never fabricated). 404 when the opportunity itself doesn't
    exist; a real opportunity with no claims yet cleanly returns `[]`."""
    opportunity = await db.get(Opportunity, opportunity_id)
    if opportunity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")

    result = await db.execute(
        select(Claim, func.count(Evidence.id))
        .outerjoin(Evidence, Evidence.claim_id == Claim.id)
        .where(Claim.opportunity_id == opportunity_id)
        .group_by(Claim.id)
        .order_by(Claim.created_at.asc())
    )
    rows = result.all()
    return [
        ClaimOut(
            id=claim.id,
            opportunity_id=claim.opportunity_id,
            claim_type=claim.claim_type,
            claim_value=claim.claim_value,
            normalized_value=claim.normalized_value,
            status=claim.status,
            importance=claim.importance,
            created_at=claim.created_at,
            updated_at=claim.updated_at,
            evidence_count=evidence_count,
        )
        for claim, evidence_count in rows
    ]


@router.get(
    "/admin/opportunities/{opportunity_id}/verification",
    response_model=VerificationDecisionOut,
)
async def get_opportunity_verification(
    opportunity_id: uuid.UUID,
    db: DbSession,
    _: RequireModerationAccess,
) -> VerificationDecisionOut:
    """Moderator-or-higher. The real, CURRENT verification decision for
    one opportunity, reconstructed read-only from its real, already-
    persisted claims/evidence via `evidence_verification.
    evaluate_verification_readonly` -- does not re-run AI extraction and
    writes nothing. 404 only when the opportunity itself doesn't exist;
    a real opportunity with no claims yet cleanly returns the same
    UNVERIFIED/"no claims" decision the live pipeline would, never a
    500."""
    opportunity = await db.get(Opportunity, opportunity_id)
    if opportunity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")

    decision = await evidence_verification.evaluate_verification_readonly(db, opportunity_id=opportunity_id)

    # D7 addition (spec section 20: "extend rather than duplicate"): the
    # opportunity's real, persisted `Contradiction` rows, scoped by this
    # same path's `opportunity_id` -- same IDOR-safe scoping every other
    # endpoint in this section uses.
    contradiction_rows = (
        await db.execute(
            select(Contradiction)
            .where(Contradiction.opportunity_id == opportunity_id)
            .order_by(Contradiction.detected_at.desc())
        )
    ).scalars().all()

    return VerificationDecisionOut(
        status=decision.status.value,
        reason=decision.reason,
        evaluated_claims=[
            EvaluatedClaimOut(
                claim_id=ev.claim_id,
                claim_type=ev.claim_type,
                status=ev.status.value,
                reason=ev.reason,
                supporting_evidence_ids=ev.supporting_evidence_ids,
                contradicting_evidence_ids=ev.contradicting_evidence_ids,
            )
            for ev in decision.evaluated_claims
        ],
        supporting_evidence_ids=decision.supporting_evidence_ids,
        contradicting_evidence_ids=decision.contradicting_evidence_ids,
        evaluated_at=decision.evaluated_at,
        verification_version=decision.verification_version,
        contradictions=[ContradictionOut.model_validate(row) for row in contradiction_rows],
    )


@router.get(
    "/admin/opportunities/{opportunity_id}/contradictions",
    response_model=list[ContradictionOut],
)
async def get_opportunity_contradictions(
    opportunity_id: uuid.UUID,
    db: DbSession,
    _: RequireModerationAccess,
) -> list[ContradictionOut]:
    """Moderator-or-higher. All real, persisted `Contradiction` rows for
    one opportunity (D7 -- Contradiction Hunter & Adversarial
    Verification), most recently detected first. 404 when the
    opportunity itself doesn't exist; a real opportunity with no
    contradictions (the overwhelming common case -- most claims are
    never contradicted) cleanly returns `[]`, never a 500.

    IDOR-safe by construction, same pattern as `get_opportunity_evidence`/
    `get_opportunity_claims` above: the query is always scoped by THIS
    path's own `opportunity_id`, never by any claim/evidence/contradiction
    id a caller could supply elsewhere -- a contradiction belonging to a
    different opportunity can never be read back through this path."""
    opportunity = await db.get(Opportunity, opportunity_id)
    if opportunity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")

    result = await db.execute(
        select(Contradiction)
        .where(Contradiction.opportunity_id == opportunity_id)
        .order_by(Contradiction.detected_at.desc())
    )
    rows = result.scalars().all()
    return [ContradictionOut.model_validate(row) for row in rows]


@router.get(
    "/admin/opportunities/evidence-stats",
    response_model=EvidenceStatsOut,
)
async def get_evidence_stats(db: DbSession, _: RequireModerationAccess) -> EvidenceStatsOut:
    """Moderator-or-higher. Real, freshly-computed DB aggregation counts
    for the D6 observability counters named in the original spec --
    plain SQLAlchemy `GROUP BY`/`COUNT(*)` queries, same posture as
    `admin_stats.py`/`moderation_stats_service.py` (no metrics library
    exists anywhere in this codebase). See `EvidenceStatsOut`'s own
    docstring for exactly which fields are exact-from-persisted-data vs.
    live-recomputed vs. honestly `None` (`evidence_duplicate_total` has
    no persisted signal at all -- see that docstring)."""
    caveats: list[str] = []

    evidence_created_total = (await db.execute(select(func.count(Evidence.id)))).scalar_one()

    # Phase 1 (Evidence Temporal Fidelity) observability: real,
    # freshly-computed counts -- see `EvidenceStatsOut`'s own docstring
    # for why the "without" count is expected to dominate today.
    evidence_with_published_at_total = (
        await db.execute(select(func.count(Evidence.id)).where(Evidence.published_at.isnot(None)))
    ).scalar_one()
    evidence_without_published_at_total = evidence_created_total - evidence_with_published_at_total

    caveats.append(
        "evidence_duplicate_total: no persisted signal exists for a dedup hit in "
        "evidence_verification.record_evidence's early-return path -- reported as null, not fabricated."
    )

    status_rows = (await db.execute(select(Claim.status, func.count(Claim.id)).group_by(Claim.status))).all()
    status_counts = {claim_status: count for claim_status, count in status_rows}

    evaluated_result = await db.execute(
        select(func.count(func.distinct(Claim.id)))
        .select_from(Claim)
        .outerjoin(Evidence, Evidence.claim_id == Claim.id)
        .where((Claim.status != EvidenceStatus.UNKNOWN.value) | (Evidence.id.isnot(None)))
    )
    claims_evaluated_total = evaluated_result.scalar_one()

    verification_verified_total = (
        await db.execute(select(func.count(Opportunity.id)).where(Opportunity.is_verified.is_(True)))
    ).scalar_one()

    # `VerificationDecision` itself is never persisted anywhere queryable
    # (only its VERIFIED outcome's side effect, `is_verified=True`, is --
    # see `EvidenceStatsOut`'s own docstring) -- live-recompute the
    # HOLD/UNVERIFIED/STALE/REJECTED breakdown over every currently-
    # unverified opportunity that has at least one real claim. Read-only,
    # nothing written.
    caveats.append(
        "verification_hold_total/verification_unverified_total/verification_stale_total/"
        "verification_rejected_total are live-recomputed via evaluate_verification_readonly over "
        "currently-unverified opportunities with real claims -- no persisted VerificationDecision "
        "field exists to query directly."
    )

    unverified_opp_ids_result = await db.execute(
        select(Claim.opportunity_id)
        .join(Opportunity, Opportunity.id == Claim.opportunity_id)
        .where(Opportunity.is_verified.is_(False))
        .distinct()
    )
    unverified_opp_ids = [row[0] for row in unverified_opp_ids_result.all()]

    hold_total = 0
    unverified_total = 0
    stale_total = 0
    rejected_total = 0
    verified_but_unflagged = 0
    for opp_id in unverified_opp_ids:
        decision = await evidence_verification.evaluate_verification_readonly(db, opportunity_id=opp_id)
        if decision.status == VerificationDecisionStatus.HOLD:
            hold_total += 1
        elif decision.status == VerificationDecisionStatus.STALE:
            stale_total += 1
        elif decision.status == VerificationDecisionStatus.REJECTED:
            rejected_total += 1
        elif decision.status == VerificationDecisionStatus.VERIFIED:
            # Recomputes VERIFIED but `is_verified` is still False -- e.g.
            # evidence changed since the pipeline last ran. Counted with
            # the persisted verified total too, since it reflects the real
            # current, recomputed truth.
            verified_but_unflagged += 1
        else:
            unverified_total += 1

    if verified_but_unflagged:
        caveats.append(
            f"{verified_but_unflagged} opportunity(ies) recompute to VERIFIED right now but "
            "is_verified is still False (evidence likely changed since the pipeline last ran) -- "
            "included in verification_verified_total."
        )

    return EvidenceStatsOut(
        evidence_created_total=evidence_created_total,
        evidence_duplicate_total=None,
        evidence_with_published_at_total=evidence_with_published_at_total,
        evidence_without_published_at_total=evidence_without_published_at_total,
        claims_evaluated_total=claims_evaluated_total,
        claims_supported_total=status_counts.get(EvidenceStatus.SUPPORTED.value, 0),
        claims_contradicted_total=status_counts.get(EvidenceStatus.CONTRADICTED.value, 0),
        claims_stale_total=status_counts.get(EvidenceStatus.STALE.value, 0),
        claims_unsupported_total=status_counts.get(EvidenceStatus.UNSUPPORTED.value, 0),
        claims_conflicted_total=status_counts.get(EvidenceStatus.CONFLICTED.value, 0),
        claims_unknown_total=status_counts.get(EvidenceStatus.UNKNOWN.value, 0),
        verification_verified_total=verification_verified_total + verified_but_unflagged,
        verification_hold_total=hold_total,
        verification_unverified_total=unverified_total,
        verification_stale_total=stale_total,
        verification_rejected_total=rejected_total,
        caveats=caveats,
    )
