"""Opportunity Monitoring & Personal Impact Engine, Part P: deterministic,
documented monitoring-priority selection.

AUDITED FIRST (Part 0): no scheduler/priority infrastructure for
re-checking ALREADY-published opportunities existed anywhere in this
codebase before this phase -- the only prior "scan opportunities"
Celery task is `aggregator_tasks.run_daily_opportunity_scraper`, which
discovers NEW opportunities from sources, not re-checks existing ones.
This module is the missing "which opportunities get re-checked, and in
what order" piece Part P asks for.

DOES NOT invent arbitrary polling intervals per opportunity (Part P's
own "do not invent arbitrary intervals without documenting them").
Instead: a single, fixed, documented scan cadence (the Celery Beat
entry in `app/core/celery_app.py`, once a day -- same conservative
cadence as the existing daily scraper) that processes a bounded BATCH
of opportunities each run, selected by real, existing signals, in this
exact priority order:

1. Opportunities with an ACTIVE, opportunity-linked `ApplicationTracker`
   row (Part P's own "actively applied-to opportunities" -- item 1).
2. Opportunities with any `UserBookmark` row (Part P's own "saved
   opportunities" -- item 2; SAVED and APPLIED bookmark statuses are
   not distinguished here since tier 1 already isolates the strongest
   "actively applying" signal).
3. Everyone else, PUBLISHED and lifecycle-OPEN only (Part Q: a
   not-yet-published or already-closed opportunity has no real user
   action riding on a fresher re-check, so it is never prioritized
   ahead of opportunities real users are actually relying on).

WITHIN each tier: oldest `monitoring_last_checked_at` first (NULLS
FIRST -- an opportunity never checked at all always outranks one
checked yesterday), so the batch makes real forward progress through
the catalog run after run rather than reprocessing the same subset
forever (see `Opportunity.monitoring_last_checked_at`'s own docstring
for why this column exists and who stamps it).

Deliberately excludes Part P's own "recently discovered"/"highly
relevant" tiers -- see the report's Known Limitations: "recently
discovered" has no existing reliable signal distinct from `created_at`
(already implicitly covered by tier 3's oldest-first ordering for a
brand-new row with `monitoring_last_checked_at IS NULL`), and "highly
relevant" would require a live per-user ranking call, which Part T's
own "no LLM/heavy call per user" performance rule forbids running once
per candidate opportunity at scheduling time.
"""

from __future__ import annotations

import uuid

from sqlalchemy import and_, case, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.application_tracker import ApplicationTracker
from app.models.bookmark import UserBookmark
from app.models.enums import OpportunityReviewStatus
from app.models.opportunity import Opportunity
from app.services.application_state_machine import INCOMPLETE_TRACKER_STATUSES

_OPEN_LIFECYCLE_STATES = ("discovered", "open", "closing_soon")


async def find_opportunities_to_monitor(db: AsyncSession, *, limit: int) -> list[uuid.UUID]:
    """Returns up to `limit` opportunity ids, highest priority first, per
    this module's own documented tier/ordering rules above. Pure
    read-only selection -- no row is touched, no monitoring is performed
    here (see `opportunity_reverification.reverify_opportunity`, the
    caller's own per-id next step)."""
    has_active_application = exists().where(
        and_(
            ApplicationTracker.opportunity_id == Opportunity.id,
            ApplicationTracker.status.in_(INCOMPLETE_TRACKER_STATUSES),
        )
    )
    has_bookmark = exists().where(UserBookmark.opportunity_id == Opportunity.id)

    # 1 = active application, 2 = bookmarked (and no active application),
    # 3 = neither (falls through to the WHERE clause's own third, open/
    # published-only condition below) -- an explicit, readable priority
    # table rather than arithmetic on cast booleans.
    tier = case(
        (has_active_application, 1),
        (has_bookmark, 2),
        else_=3,
    ).label("tier")

    result = await db.execute(
        select(Opportunity.id)
        .where(
            Opportunity.review_status == OpportunityReviewStatus.PUBLISHED.value,
            or_(
                has_active_application,
                has_bookmark,
                Opportunity.lifecycle_state.in_(_OPEN_LIFECYCLE_STATES),
            ),
        )
        .order_by(tier, Opportunity.monitoring_last_checked_at.asc().nulls_first())
        .limit(limit)
    )
    return [row[0] for row in result.all()]
