"""Personalized Opportunity Intelligence + Opportunity Passport step,
Part B/C/D: a small, deterministic eligibility evaluation layer.

CONSTITUTION (Part R, enforced structurally): `evaluate_eligibility`
takes only already-persisted, structured `User`/`Opportunity` columns as
input -- never raw source text, never an LLM call, never a confidence
score. There is no path from an opportunity's scraped description or a
user's free-text bio into this function's control flow. AI may assist
elsewhere (semantic interest mapping, explanation phrasing -- see
`personalized_ranking.py`), but eligibility itself is decided here, by
plain comparisons against real column values, full stop.

REUSES, NEVER DUPLICATES, the existing hard-eligibility rule set already
established in `app/api/v1/endpoints/opportunities.py::
_build_eligibility_filters` and `app/api/v1/endpoints/dashboards.py::
_build_opportunity_filters` (confirmed via a direct read of both before
writing this module -- Part 0's own audit instruction): the same four
dimensions those two endpoints already filter on in SQL (age via
`min_age`/`max_age`, academic level via `target_academic_levels`,
gender via `target_gender`, plus this module's own new residency/
nationality dimension) are evaluated here too, with the SAME "a missing
signal on either side excludes nothing" posture -- but re-expressed as
a structured, per-rule, explainable `EligibilityResult` instead of a
collapsed SQL `WHERE` clause, so a caller can see WHY, not just
filter/no-filter.
"""

from __future__ import annotations

import dataclasses

from app.models.enums import EligibilityStatus
from app.models.opportunity import Opportunity
from app.models.user import User

# One evaluated eligibility dimension -- age, academic level, gender,
# or residency/nationality. `rule` is a short machine-readable key (used
# by both `matched_rules`/`failed_rules`/`unknown_rules` bucketing and
# by the Passport's own "what is missing" section); `explanation` is a
# real, human-readable sentence built from the actual compared values,
# never invented text (Part D's own "do not fabricate" rule).
@dataclasses.dataclass
class RuleEvaluation:
    rule: str
    explanation: str


@dataclasses.dataclass
class EligibilityResult:
    status: EligibilityStatus
    matched_rules: list[RuleEvaluation]
    failed_rules: list[RuleEvaluation]
    unknown_rules: list[RuleEvaluation]
    missing_information: list[str]


def _evaluate_age(user: User, opportunity: Opportunity) -> RuleEvaluation | None:
    """`None` return means this dimension has NO stated requirement at
    all on the opportunity (both `min_age`/`max_age` unset) -- it is
    simply not a rule this opportunity has, not evaluated as any status
    at all (matches `_build_eligibility_filters`'s own "opportunity side
    missing means unrestricted" posture)."""
    if opportunity.min_age is None and opportunity.max_age is None:
        return None
    if user.age is None:
        bounds = []
        if opportunity.min_age is not None:
            bounds.append(f"minimum age {opportunity.min_age}")
        if opportunity.max_age is not None:
            bounds.append(f"maximum age {opportunity.max_age}")
        return RuleEvaluation("age", f"unknown: user age not stated (opportunity requires {' and '.join(bounds)})")
    if opportunity.min_age is not None and user.age < opportunity.min_age:
        return RuleEvaluation("age", f"user age {user.age} < minimum age {opportunity.min_age}")
    if opportunity.max_age is not None and user.age > opportunity.max_age:
        return RuleEvaluation("age", f"user age {user.age} > maximum age {opportunity.max_age}")
    return RuleEvaluation("age", f"user age {user.age} satisfies opportunity age requirement")


def _evaluate_academic_level(user: User, opportunity: Opportunity) -> RuleEvaluation | None:
    if not opportunity.target_academic_levels:
        return None
    if user.academic_level is None:
        levels = ", ".join(lvl.value for lvl in opportunity.target_academic_levels)
        return RuleEvaluation(
            "academic_level", f"unknown: user academic level not stated (opportunity requires one of: {levels})"
        )
    if user.academic_level in opportunity.target_academic_levels:
        return RuleEvaluation("academic_level", f"user academic level {user.academic_level.value} matches")
    levels = ", ".join(lvl.value for lvl in opportunity.target_academic_levels)
    return RuleEvaluation(
        "academic_level", f"user academic level {user.academic_level.value} not in required set: {levels}"
    )


def _evaluate_gender(user: User, opportunity: Opportunity) -> RuleEvaluation | None:
    if opportunity.target_gender is None:
        return None
    if user.gender is None:
        return RuleEvaluation(
            "gender", f"unknown: user gender not stated (opportunity requires {opportunity.target_gender.value})"
        )
    if user.gender == opportunity.target_gender:
        return RuleEvaluation("gender", f"user gender {user.gender.value} matches")
    return RuleEvaluation(
        "gender", f"user gender {user.gender.value} does not match required {opportunity.target_gender.value}"
    )


def _evaluate_residency(user: User, opportunity: Opportunity) -> RuleEvaluation | None:
    """Reuses `Opportunity.country`/`is_local_egypt` -- the same two
    real columns `dashboards.py` already treats as this opportunity's
    geography restriction -- compared against the user's own
    `residency_country` (falling back to `nationality` when residency
    itself isn't stated, since either is a legitimate real-world basis
    for many eligibility rules, e.g. "Egyptian nationals" vs. "residents
    of Egypt"). `country == 'Global'` (this table's own existing
    server-side default, see `Opportunity`'s class docstring) is treated
    as no real restriction, same as `country IS NULL`."""
    if opportunity.country is None or opportunity.country == "Global":
        return None
    user_geo = user.residency_country or user.nationality
    if user_geo is None:
        return RuleEvaluation(
            "residency",
            f"unknown: user residency/nationality not stated (opportunity requires {opportunity.country})",
        )
    if user_geo.strip().casefold() == opportunity.country.strip().casefold():
        return RuleEvaluation("residency", f"user residency/nationality {user_geo} matches {opportunity.country}")
    return RuleEvaluation(
        "residency", f"user residency/nationality {user_geo} does not match required {opportunity.country}"
    )


def evaluate_eligibility(user: User, opportunity: Opportunity) -> EligibilityResult:
    """Deterministically evaluates one user against one opportunity's
    real, structured eligibility columns.

    STATUS RULES (Part B's own explicit examples):
    - Any `failed_rules` entry (a real, stated mismatch) -> INELIGIBLE.
      A single hard failure is disqualifying regardless of how many
      other dimensions matched -- ranking can never "rank around" it
      (Part C's own explicit rule, enforced downstream in
      `personalized_ranking.py`, not here).
    - No failures, but at least one `unknown_rules` entry AND at least
      one real `matched_rules` entry -> CONDITIONAL (partially
      established, partially missing).
    - No failures, no matches, only unknowns (or the opportunity states
      no requirements at all AND the user has stated nothing relevant)
      -> UNKNOWN if there is at least one real requirement this
      opportunity states that could not be evaluated; otherwise, with
      zero stated requirements on the opportunity at all, ELIGIBLE (an
      opportunity with no eligibility restrictions is open to everyone,
      a real, meaningful conclusion, not an absence of one).
    - No failures, no unknowns, at least one match (or zero
      requirements stated at all) -> ELIGIBLE.
    """
    evaluations = [
        _evaluate_age(user, opportunity),
        _evaluate_academic_level(user, opportunity),
        _evaluate_gender(user, opportunity),
        _evaluate_residency(user, opportunity),
    ]
    real_evaluations = [e for e in evaluations if e is not None]

    matched_rules: list[RuleEvaluation] = []
    failed_rules: list[RuleEvaluation] = []
    unknown_rules: list[RuleEvaluation] = []

    for evaluation in real_evaluations:
        if evaluation.explanation.startswith("unknown:"):
            unknown_rules.append(evaluation)
        elif "does not match" in evaluation.explanation or " < " in evaluation.explanation or " > " in evaluation.explanation or "not in required set" in evaluation.explanation:
            failed_rules.append(evaluation)
        else:
            matched_rules.append(evaluation)

    missing_information = [
        e.rule.replace("_", " ") for e in unknown_rules
    ]

    if failed_rules:
        status = EligibilityStatus.INELIGIBLE
    elif unknown_rules and matched_rules:
        status = EligibilityStatus.CONDITIONAL
    elif unknown_rules:
        status = EligibilityStatus.UNKNOWN
    else:
        status = EligibilityStatus.ELIGIBLE

    return EligibilityResult(
        status=status,
        matched_rules=matched_rules,
        failed_rules=failed_rules,
        unknown_rules=unknown_rules,
        missing_information=missing_information,
    )
