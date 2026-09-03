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
from app.models.enums import ModerationAction, ModerationTargetType, OpportunityReviewStatus, SourceType
from app.models.opportunity import Opportunity
from app.models.user import User
from app.schemas.opportunity import (
    AdminOpportunityCreate,
    AdminOpportunityDecisionRequest,
    AdminOpportunityDecisionResponse,
    AdminOpportunityReviewOut,
    OpportunityOut,
    PaginatedResponse,
)
from app.services import moderation_claim_service, moderation_log_service
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
