"""Personalized Opportunity Intelligence + Opportunity Passport step,
Part G/H/I/J/K/L/S: explainable personalization signals, a safe ranking
pipeline, "why this matches you" / "what is missing" explanations, and a
configurable exploration ratio.

NO SCORE THEATER (Part S): every signal below is a `MatchLevel`
(NONE/LOW/MEDIUM/HIGH/STRONG), a plain, explainable bucket -- never a
bare, unexplained percentage. `personalization_score` (an optional,
purely-derived float used only to order results within a tier) is a
DOCUMENTED, REPRODUCIBLE weighted count of non-NONE signals, never an
opaque model output.

RANKING SAFETY (Part H): ranking is a strict, ordered pipeline --
INELIGIBLE and CLOSED opportunities are removed BEFORE any
personalization signal is even computed, so no signal can ever "rank
around" a hard failure (structurally, not by convention: `rank_candidates`
never calls `compute_match_signals` for a candidate that failed the hard
filter).

PRIVACY (Part M): none of `gender`/`nationality`/`residency_country`/
`age` are read by ANY function in this module -- those are hard-
eligibility-only facts (`eligibility_engine.py`), never personalization
or ranking inputs, and never appear in an explanation string. Verified
directly by this module's own test suite
(`tests/test_personalized_ranking.py::TestPrivacyBoundary`).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import random
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entity_relationship import EntityRelationship
from app.models.enums import EligibilityStatus, MatchLevel
from app.services.opportunity_state_machine import USER_FACING_EXCLUDED_LIFECYCLE_STATES
from app.models.opportunity import Opportunity
from app.models.user import User
from app.services import funding_intelligence
from app.services.eligibility_engine import EligibilityResult, evaluate_eligibility

# Part L's own configurable exploration ratio -- the fraction of a
# result page reserved for opportunities that pass hard eligibility but
# did NOT rank in the top strong-match tier, so a user's feed never
# narrows to only what they've already shown interest in. Module-level
# constant (not a hardcoded literal inside the ranking function) so a
# caller/test can override it explicitly -- see `rank_candidates`'s own
# `exploration_ratio` parameter, defaulted to this value.
DEFAULT_EXPLORATION_RATIO = 0.2


@dataclasses.dataclass
class MatchSignals:
    """One explainable personalization signal per dimension (Part G's
    own literal list, minus `skills_match`/`education_match`, which are
    Part A profile concepts this phase's `User` model does not yet
    structurally distinguish from `interests`/`academic_level` -- see
    the report's Known Limitations for the disclosed reasoning)."""

    interest_match: MatchLevel
    goal_match: MatchLevel
    type_match: MatchLevel
    geography_match: MatchLevel
    funding_match: MatchLevel
    deadline_match: MatchLevel
    graph_relevance: MatchLevel


@dataclasses.dataclass
class RankedOpportunity:
    opportunity: Opportunity
    eligibility: EligibilityResult
    signals: MatchSignals
    personalization_score: float
    reasons: list[str]
    missing_information: list[str]
    is_exploration: bool


_LEVEL_WEIGHT = {
    MatchLevel.NONE: 0.0,
    MatchLevel.LOW: 1.0,
    MatchLevel.MEDIUM: 2.0,
    MatchLevel.HIGH: 3.0,
    MatchLevel.STRONG: 4.0,
}


def _interest_overlap_level(user_terms: list[str] | None, opportunity_text: str) -> MatchLevel:
    """Deterministic, case-insensitive substring match between the
    user's own stated interests and the opportunity's title+description
    -- no embedding call, no LLM call (Part R). A genuinely simple
    signal, disclosed as such: it will miss a semantically-related but
    differently-worded interest (e.g. "AI" vs. "machine learning") --
    see Known Limitations."""
    if not user_terms:
        return MatchLevel.NONE
    text_lower = opportunity_text.lower()
    hits = sum(1 for term in user_terms if term.strip() and term.strip().lower() in text_lower)
    if hits == 0:
        return MatchLevel.NONE
    if hits == 1:
        return MatchLevel.MEDIUM
    return MatchLevel.STRONG


def _goal_match_level(user_goals: list[str] | None, opportunity_type_value: str) -> MatchLevel:
    """A real, small, documented mapping from each `ApplicationGoal` to
    the `OpportunityType` values that goal is realistically satisfied
    by -- deterministic, not inferred by an LLM."""
    if not user_goals:
        return MatchLevel.NONE
    goal_to_types = {
        "study": {"scholarship", "masters", "phd", "bachelors", "exchange_program"},
        "research": {"research_funding", "research_program", "research_internship", "fellowship", "grant"},
        "career": {"internship", "job", "company_training", "certification"},
        "entrepreneurship": {"accelerator", "incubator", "startup_funding", "project_funding"},
        "volunteering": {"volunteer", "youth_program"},
        "competitions": {"competition"},
        "networking": {"conference", "exchange_program", "youth_program"},
    }
    matched_goals = [g for g in user_goals if opportunity_type_value in goal_to_types.get(g, set())]
    if not matched_goals:
        return MatchLevel.NONE
    return MatchLevel.HIGH if len(matched_goals) == 1 else MatchLevel.STRONG


def _type_match_level(preferred_types: list | None, opportunity_type_value: str) -> MatchLevel:
    if not preferred_types:
        return MatchLevel.NONE
    preferred_values = {t.value if hasattr(t, "value") else t for t in preferred_types}
    return MatchLevel.STRONG if opportunity_type_value in preferred_values else MatchLevel.NONE


def _geography_match_level(user: User, opportunity: Opportunity) -> MatchLevel:
    """Preference-based, NOT the hard-eligibility residency check
    (Part M: nationality/residency itself is never used here) --
    `User.target_country` is this user's own STATED destination
    PREFERENCE (see `User.target_country`'s own docstring), a real,
    non-sensitive signal distinct from where they live."""
    if not user.target_country:
        return MatchLevel.NONE
    if opportunity.country is None or opportunity.country == "Global":
        return MatchLevel.LOW  # open to everywhere, including their preference, but not a targeted match
    if user.target_country.strip().casefold() == opportunity.country.strip().casefold():
        return MatchLevel.STRONG
    return MatchLevel.NONE


def _funding_match_level(user: User, opportunity: Opportunity) -> MatchLevel:
    if not user.funding_preference:
        return MatchLevel.NONE
    funding_types = funding_intelligence.classify_funding_text(opportunity.funding_details)
    preference_values = {t.value for t in funding_types}
    return MatchLevel.HIGH if user.funding_preference in preference_values else MatchLevel.NONE


def _deadline_match_level(user: User, opportunity: Opportunity, now: dt.datetime) -> MatchLevel:
    """Reuses the opportunity's own real, already-parsed `deadline` --
    no new date logic, no retrieval-time inference (same discipline as
    `opportunity_state_machine.py`)."""
    if opportunity.deadline is None:
        return MatchLevel.NONE
    deadline = opportunity.deadline if opportunity.deadline.tzinfo else opportunity.deadline.replace(tzinfo=dt.timezone.utc)
    days_remaining = (deadline - now).days
    if days_remaining < 0:
        return MatchLevel.NONE
    lead_time = user.deadline_lead_time_days
    if lead_time is None:
        return MatchLevel.LOW if days_remaining >= 0 else MatchLevel.NONE
    return MatchLevel.HIGH if days_remaining >= lead_time else MatchLevel.LOW


async def _graph_relevance_levels(
    db: AsyncSession, *, candidate_ids: list[uuid.UUID], bookmarked_ids: set[uuid.UUID]
) -> dict[uuid.UUID, MatchLevel]:
    """Part K: reuses the real, existing D2 Knowledge Graph
    (`entity_relationships`, Deliverable 2) -- no new graph, no new
    edges. A candidate opportunity is HIGH graph relevance when it has a
    real edge to an opportunity the user has already bookmarked (a
    concrete signal the user is already engaged with something related);
    NONE otherwise. One bulk query for the whole candidate set, not one
    query per candidate (Part P: bounded, not per-item)."""
    if not candidate_ids or not bookmarked_ids:
        return {cid: MatchLevel.NONE for cid in candidate_ids}

    result = await db.execute(
        select(EntityRelationship.opportunity_id)
        .where(
            EntityRelationship.opportunity_id.in_(candidate_ids),
            EntityRelationship.related_opportunity_id.in_(bookmarked_ids),
        )
        .distinct()
    )
    related_candidate_ids = {row[0] for row in result.all()}
    return {
        cid: (MatchLevel.HIGH if cid in related_candidate_ids else MatchLevel.NONE) for cid in candidate_ids
    }


async def compute_match_signals(
    db: AsyncSession,
    *,
    user: User,
    opportunity: Opportunity,
    now: dt.datetime,
    graph_relevance: MatchLevel = MatchLevel.NONE,
) -> MatchSignals:
    opportunity_text = f"{opportunity.title} {opportunity.description or ''}"
    return MatchSignals(
        interest_match=_interest_overlap_level(user.interests, opportunity_text),
        goal_match=_goal_match_level(user.application_goals, opportunity.type.value),
        type_match=_type_match_level(user.preferred_opportunity_types, opportunity.type.value),
        geography_match=_geography_match_level(user, opportunity),
        funding_match=_funding_match_level(user, opportunity),
        deadline_match=_deadline_match_level(user, opportunity, now),
        graph_relevance=graph_relevance,
    )


def personalization_score(signals: MatchSignals) -> float:
    """REPRODUCIBLE, DOCUMENTED formula (Part S): the unweighted sum of
    each signal's `_LEVEL_WEIGHT`. Every component is independently
    visible via `MatchSignals` itself -- this score exists only to
    order results within a tier, never to justify a claim like "93%
    match" (never presented to the user as a percentage anywhere in
    this codebase)."""
    return sum(
        _LEVEL_WEIGHT[level]
        for level in (
            signals.interest_match, signals.goal_match, signals.type_match,
            signals.geography_match, signals.funding_match, signals.deadline_match,
            signals.graph_relevance,
        )
    )


def build_explanation(eligibility: EligibilityResult, signals: MatchSignals) -> list[str]:
    """Part I: structured reasons, built ONLY from real signal values --
    never invented by an LLM (Part I's own explicit rule)."""
    reasons: list[str] = []
    if eligibility.status == EligibilityStatus.ELIGIBLE:
        reasons.append("You appear eligible for this opportunity.")
    label_map = {
        "interest_match": "Matches your stated interests",
        "goal_match": "Matches your application goals",
        "type_match": "Matches your preferred opportunity type",
        "geography_match": "Matches your preferred destination",
        "funding_match": "Matches your funding preference",
        "deadline_match": "Deadline fits your stated lead-time preference",
        "graph_relevance": "Related to an opportunity you've already bookmarked",
    }
    for field, label in label_map.items():
        level = getattr(signals, field)
        if level in (MatchLevel.HIGH, MatchLevel.STRONG):
            reasons.append(f"✓ {label} ({level.value})")
    return reasons


def build_missing_information(eligibility: EligibilityResult) -> list[str]:
    """Part J: explicit missing-information list for UNKNOWN/CONDITIONAL
    eligibility -- never silently converted into false eligibility."""
    if not eligibility.unknown_rules:
        return []
    return [f"APEX needs your {rule.rule.replace('_', ' ')} to fully confirm eligibility." for rule in eligibility.unknown_rules]


async def rank_candidates(
    db: AsyncSession,
    *,
    user: User,
    opportunities: list[Opportunity],
    bookmarked_opportunity_ids: set[uuid.UUID],
    now: dt.datetime | None = None,
    exploration_ratio: float = DEFAULT_EXPLORATION_RATIO,
    limit: int = 20,
    rng: random.Random | None = None,
) -> list[RankedOpportunity]:
    """Part H's own safe pipeline, exactly:

    1. Remove clearly INELIGIBLE.
    2. Remove CLOSED/HOLD/CONFLICT (lifecycle state; F-HOLDFILTER repair).
    3. Keep ELIGIBLE separately from UNKNOWN/CONDITIONAL (never mixed
       silently -- both are returned, each item carries its own real
       `eligibility.status`, so a caller/UI can always tell which is
       which; nothing here re-labels one as the other).
    4. Calculate personalization signals only for what survives 1-2.
    5. Rank by personalization score, descending.
    6. Reserve `exploration_ratio` of the final `limit` for real,
       hard-eligible candidates OUTSIDE the top-ranked tier (Part L) --
       still 100% hard-eligibility-passing, never a bypass of Part H.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    rng = rng or random.Random()

    # F-HOLDFILTER repair (P0-A): previously excluded CLOSED only --
    # HOLD/CONFLICT opportunities (D6 evidence insufficient/contradicted,
    # or an unresolved authoritative contradiction) passed straight
    # through to ranking/display, exactly like a fully OPEN opportunity.
    # Now excludes every state the shared, single-source-of-truth
    # `USER_FACING_EXCLUDED_LIFECYCLE_STATES` set says a user-facing
    # surface must never show.
    survivors = [
        o for o in opportunities
        if o.lifecycle_state not in USER_FACING_EXCLUDED_LIFECYCLE_STATES
    ]

    candidate_ids = [o.id for o in survivors]
    graph_levels = await _graph_relevance_levels(
        db, candidate_ids=candidate_ids, bookmarked_ids=bookmarked_opportunity_ids
    )

    scored: list[RankedOpportunity] = []
    for opportunity in survivors:
        eligibility = evaluate_eligibility(user, opportunity)
        if eligibility.status == EligibilityStatus.INELIGIBLE:
            continue  # Part H: never promoted, never scored, never shown as a ranked result.

        signals = await compute_match_signals(
            db, user=user, opportunity=opportunity, now=now, graph_relevance=graph_levels.get(opportunity.id, MatchLevel.NONE)
        )
        score = personalization_score(signals)
        scored.append(
            RankedOpportunity(
                opportunity=opportunity,
                eligibility=eligibility,
                signals=signals,
                personalization_score=score,
                reasons=build_explanation(eligibility, signals),
                missing_information=build_missing_information(eligibility),
                is_exploration=False,
            )
        )

    scored.sort(key=lambda r: r.personalization_score, reverse=True)

    exploration_slots = max(0, round(limit * exploration_ratio)) if scored else 0
    core_slots = max(0, limit - exploration_slots)

    core = scored[:core_slots]
    remainder = scored[core_slots:]
    exploration_pool = list(remainder)
    rng.shuffle(exploration_pool)
    exploration = exploration_pool[:exploration_slots]
    for item in exploration:
        item.is_exploration = True

    return (core + exploration)[:limit]
