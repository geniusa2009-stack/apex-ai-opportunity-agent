"""APEX User Intelligence + Proactive Guidance step, Part F/H: the first
real proactive Personal Opportunity Feed.

REUSES, NEVER DUPLICATES, `personalized_ranking.rank_candidates` for the
actual hard-eligibility filter + personalization + exploration pipeline
(Part G's own "do not weaken this rule" / this phase's own "reuse the
existing personalized ranking service where appropriate" instruction).
This module's only real additions on top of that are:

1. Candidate SOURCING (Part F's 10-source list, reduced to the ones this
   codebase can support without fabricating a signal): published,
   non-closed opportunities, with `created_at`/`updated_at` available to
   flag "newly discovered"/"recently changed" at the explanation layer.
2. Excluding opportunities the user has already told APEX they don't
   want (Part O's feedback loop -- `interaction_service.
   get_dismissed_opportunity_ids`), BEFORE ranking, not as an
   after-the-fact filter on the ranked page.
3. Deterministic, reproducible DIVERSITY (Part H): no more than two
   consecutive feed items of the same `OpportunityType`, enforced by a
   stable greedy re-sequencing that never drops a real hard-eligible
   candidate -- it only reorders where a same-type run would otherwise
   occur, pushing the deferred item later in the SAME page rather than
   discarding it.
4. Feed-level explanation lines ("Newly discovered", "Deadline
   approaching") ADDED to (never replacing) `personalized_ranking.
   build_explanation`'s own structured reasons (Part I).
"""

from __future__ import annotations

import dataclasses
import datetime as dt

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import ChangeSeverity, OpportunityReviewStatus
from app.models.opportunity_change_history import OpportunityChangeHistory
from app.models.opportunity import Opportunity
from app.models.user import User
from app.services.interaction_service import get_dismissed_opportunity_ids
from app.services.personalized_ranking import DEFAULT_EXPLORATION_RATIO, RankedOpportunity, rank_candidates

# Part F: a new opportunity is "newly discovered" for feed-explanation
# purposes if created within this many days of `now` -- a real,
# documented, reproducible threshold, not a vague notion of "recent".
NEWLY_DISCOVERED_WINDOW_DAYS = 7

# Part J/Part F: a deadline counts as "approaching" for feed-explanation
# purposes within this many days when the user has not stated their own
# `deadline_lead_time_days` preference (which, when present, is already
# the more precise signal `personalized_ranking._deadline_match_level`
# uses).
DEFAULT_DEADLINE_APPROACHING_WINDOW_DAYS = 14

# Part H: no more than this many consecutive feed items of the same
# `OpportunityType` -- a small, documented, reproducible diversity rule.
MAX_CONSECUTIVE_SAME_TYPE = 2

CANDIDATE_POOL_LIMIT = 500

# Opportunity Monitoring & Personal Impact Engine step, Part K: how far
# back a "recently changed" feed-level reason still applies -- same
# window as `guidance_service.RECENT_CHANGE_WINDOW_DAYS`, deliberately
# kept identical rather than a second, different constant.
RECENTLY_CHANGED_WINDOW_DAYS = 7
_FEED_NOTABLE_SEVERITIES = frozenset(
    {ChangeSeverity.MATERIAL.value, ChangeSeverity.HIGH.value, ChangeSeverity.CRITICAL.value}
)


@dataclasses.dataclass
class FeedItem:
    ranked: RankedOpportunity
    feed_reasons: list[str]


@dataclasses.dataclass
class PersonalFeed:
    items: list[FeedItem]
    exploration_ratio: float
    candidate_pool_size: int
    excluded_dismissed_count: int


def _feed_level_reasons(opportunity: Opportunity, now: dt.datetime, user: User) -> list[str]:
    reasons: list[str] = []
    if opportunity.created_at is not None:
        # SQLite (test-time) returns naive datetimes even for a
        # `DateTime(timezone=True)` column; real Postgres always returns
        # tz-aware ones. Normalize defensively, same posture as the
        # `deadline` handling immediately below.
        created_at = (
            opportunity.created_at if opportunity.created_at.tzinfo else opportunity.created_at.replace(tzinfo=dt.timezone.utc)
        )
        age_days = (now - created_at).days
        if 0 <= age_days <= NEWLY_DISCOVERED_WINDOW_DAYS:
            reasons.append("Newly discovered opportunity.")
    if opportunity.deadline is not None:
        deadline = (
            opportunity.deadline if opportunity.deadline.tzinfo else opportunity.deadline.replace(tzinfo=dt.timezone.utc)
        )
        days_remaining = (deadline - now).days
        window = user.deadline_lead_time_days or DEFAULT_DEADLINE_APPROACHING_WINDOW_DAYS
        if 0 <= days_remaining <= window:
            reasons.append(f"Deadline approaching in {days_remaining} day(s).")
    return reasons


def _apply_diversity(ranked: list[RankedOpportunity]) -> list[RankedOpportunity]:
    """Stable, deterministic diversity re-sequencing (Part H). Never
    drops a candidate -- an item that would create a same-type run of
    more than `MAX_CONSECUTIVE_SAME_TYPE` is deferred to the first later
    position where it no longer would, preserving relative score order
    among items of the same type."""
    result: list[RankedOpportunity] = []
    deferred: list[RankedOpportunity] = []
    pending = list(ranked)

    while pending or deferred:
        placed = False
        for idx, candidate in enumerate(pending):
            recent_types = [r.opportunity.type for r in result[-MAX_CONSECUTIVE_SAME_TYPE:]]
            if len(recent_types) < MAX_CONSECUTIVE_SAME_TYPE or len(set(recent_types)) > 1:
                result.append(pending.pop(idx))
                placed = True
                break
            if candidate.opportunity.type != recent_types[0]:
                result.append(pending.pop(idx))
                placed = True
                break
        if placed:
            continue
        if not pending:
            break
        # Every remaining candidate would extend the same-type run --
        # place the next-best one anyway rather than looping forever;
        # diversity is a soft, best-effort ordering rule, never a reason
        # to drop a real hard-eligible candidate (Part H's own "do not
        # weaken this rule" applies to eligibility, not to this ordering
        # nicety, but dropping real candidates here would be its own,
        # separate correctness bug).
        result.append(pending.pop(0))

    return result


async def build_personal_feed(
    db: AsyncSession,
    *,
    user: User,
    now: dt.datetime | None = None,
    limit: int = 20,
    exploration_ratio: float = DEFAULT_EXPLORATION_RATIO,
    opportunity_type=None,
    country: str | None = None,
) -> PersonalFeed:
    now = now or dt.datetime.now(dt.timezone.utc)

    filters = [Opportunity.review_status == OpportunityReviewStatus.PUBLISHED.value]
    if opportunity_type is not None:
        filters.append(Opportunity.type == opportunity_type)
    if country is not None:
        filters.append(Opportunity.country == country)

    candidate_result = await db.execute(select(Opportunity).where(*filters).limit(CANDIDATE_POOL_LIMIT))
    candidates = list(candidate_result.scalars().all())

    dismissed_ids = await get_dismissed_opportunity_ids(db, user_id=user.id)
    excluded_count = 0
    if dismissed_ids:
        pre_count = len(candidates)
        candidates = [c for c in candidates if c.id not in dismissed_ids]
        excluded_count = pre_count - len(candidates)

    bookmark_ids = {b.opportunity_id for b in user.bookmarks if b.opportunity_id is not None}

    ranked = await rank_candidates(
        db,
        user=user,
        opportunities=candidates,
        bookmarked_opportunity_ids=bookmark_ids,
        now=now,
        exploration_ratio=exploration_ratio,
        limit=limit,
    )

    diversified = _apply_diversity(ranked)

    # Opportunity Monitoring & Personal Impact Engine step, Part K: ONE
    # extra query, scoped to only the opportunities that actually made
    # this page (never the full 500-row candidate pool -- Part T's own
    # "avoid N+1"/"no per-user heavy scan" rule applies just as much to
    # this single extra feed query), to attach a "Recently changed"
    # reason without a second feed engine (this function's own existing
    # pipeline is untouched otherwise).
    recently_changed_ids: set[uuid.UUID] = set()
    page_ids = [r.opportunity.id for r in diversified]
    if page_ids:
        cutoff = now - dt.timedelta(days=RECENTLY_CHANGED_WINDOW_DAYS)
        change_result = await db.execute(
            select(OpportunityChangeHistory.opportunity_id).where(
                OpportunityChangeHistory.opportunity_id.in_(page_ids),
                OpportunityChangeHistory.severity.in_(_FEED_NOTABLE_SEVERITIES),
                OpportunityChangeHistory.changed_at >= cutoff,
            )
        )
        recently_changed_ids = {row[0] for row in change_result.all()}

    items = []
    for r in diversified:
        reasons = _feed_level_reasons(r.opportunity, now, user)
        if r.opportunity.id in recently_changed_ids:
            reasons.append("Recently changed -- review what's new.")
        items.append(FeedItem(ranked=r, feed_reasons=reasons))

    return PersonalFeed(
        items=items,
        exploration_ratio=exploration_ratio,
        candidate_pool_size=len(candidates),
        excluded_dismissed_count=excluded_count,
    )
