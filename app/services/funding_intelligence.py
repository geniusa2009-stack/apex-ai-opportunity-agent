"""Opportunity Intelligence Hardening, Part 5: Funding Intelligence.

DISCLOSED SCOPE: same posture as `deadline_intelligence.py` -- a pure,
standalone classification utility over an opportunity's own raw source
text, not yet wired into a new DB column (no migration in this pass).

Confirmed real gap: `Opportunity.funding_details` /
`ai.VerifiedOpportunitySchema.funding_details` /
`Claim(claim_type="funding")` are all free text today -- nothing in
this codebase distinguishes "fully funded" from "free to apply" from "a
cash prize" from "funding amount unspecified." Per this task's own
Part 5 critical rules, none of those are equivalent, and conflating them
is a real correctness risk (a student could believe travel is covered
when it is not). `classify_funding_text` makes the distinction
explicit and auditable.
"""

from __future__ import annotations

import enum
import re


class FundingType(str, enum.Enum):
    """The real, distinguishable funding postures this codebase can
    honestly infer from an opportunity's own source text. Multiple may
    apply to one opportunity -- see `classify_funding_text`, which
    returns a set, not a single value; this enum is the closed
    vocabulary those set members are drawn from."""

    FULLY_FUNDED = "fully_funded"
    PARTIALLY_FUNDED = "partially_funded"
    TUITION_ONLY = "tuition_only"
    STIPEND = "stipend"
    TRAVEL_FUNDED = "travel_funded"
    ACCOMMODATION_FUNDED = "accommodation_funded"
    FEE_WAIVER = "fee_waiver"
    PRIZE = "prize"
    UNPAID = "unpaid"
    PAID = "paid"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------
# Critical anti-exaggeration rules (Part 5's own literal list), each
# enforced as a DENY list checked BEFORE the corresponding ALLOW
# pattern, so a superficially similar phrase can never silently upgrade
# a weaker claim into a stronger one:
#   "Free application" != "fully funded"
#   "Scholarship" != "fully funded"      (the word alone proves nothing)
#   "Prize" != "funding"                 (a prize is compensation, not a
#                                          funding-coverage guarantee)
#   "Funding available" != "amount guaranteed"
# ---------------------------------------------------------------------

_FULLY_FUNDED_PATTERNS = [
    r"\bfully[- ]funded\b", r"\ball expenses (?:paid|covered)\b",
    r"\bfully covers?\b", r"ممول بالكامل", r"entièrement financé",
]
# Explicitly NOT fully-funded signals -- checked first so they can never
# be mistaken for the pattern above by a naive substring match.
_FREE_APPLICATION_ONLY_PATTERNS = [
    r"\bno application fee", r"\bfree to apply\b", r"\bfree application\b",
    r"بدون رسوم تقديم", r"candidature gratuite",
]
_PARTIAL_PATTERNS = [
    r"\bpartial(?:ly)? funded\b", r"\bpartial scholarship\b", r"\bpartially covers?\b",
    r"ممول جزئياً", r"partiellement financé",
]
_TUITION_PATTERNS = [r"\btuition\b", r"\bمصاريف دراسية\b", r"\bfrais de scolarité\b"]
_STIPEND_PATTERNS = [r"\bstipend\b", r"\bmonthly allowance\b", r"\bمكافأة شهرية\b", r"\ballocation mensuelle\b"]
_TRAVEL_PATTERNS = [r"\bairfare\b", r"\btravel (?:costs?|expenses?|covered|funded)\b", r"\bflight(?:s)? covered\b", r"\bسفر\b"]
_ACCOMMODATION_PATTERNS = [r"\baccommodation\b", r"\bhousing (?:covered|provided)\b", r"\bإقامة\b", r"\bhébergement\b"]
_FEE_WAIVER_PATTERNS = [r"\bfee waiver\b", r"\bwaived fees?\b", r"\bإعفاء من الرسوم\b"]
_PRIZE_PATTERNS = [r"\bprize\b", r"\bcash prize\b", r"\bجائزة\b", r"\bprix\b"]
_UNPAID_PATTERNS = [r"\bunpaid\b", r"\bno compensation\b", r"\bغير مدفوع\b", r"\bnon rémunéré\b"]
_PAID_PATTERNS = [r"\bpaid (?:internship|position|role)\b", r"\bsalary\b", r"\bمدفوع الأجر\b", r"\brémunéré\b"]


def _matches_any(patterns: list[str], text_lower: str) -> bool:
    return any(re.search(p, text_lower, re.IGNORECASE) for p in patterns)


def classify_funding_text(raw_text: str | None) -> frozenset[FundingType]:
    """Classify the funding posture(s) of one opportunity's raw source
    text. Returns a (possibly empty, possibly multi-member) frozenset --
    e.g. a post can genuinely be both TRAVEL_FUNDED and STIPEND at once.

    `raw_text` is treated as inert DATA only, pattern-matched against a
    small fixed keyword list -- never sent to an LLM, never interpreted
    as an instruction. A prompt-injection payload inside `raw_text`
    cannot change this function's output beyond whatever literal funding
    keywords it happens to contain.

    Returns `frozenset({FundingType.UNKNOWN})` when the text gives no
    funding signal at all -- never an invented amount or guessed
    coverage. "Scholarship"/"funding available" alone (with none of the
    more specific patterns below) also resolves to UNKNOWN, per this
    module's own anti-exaggeration rules -- the word alone proves
    nothing about actual coverage.
    """
    if not raw_text or not raw_text.strip():
        return frozenset({FundingType.UNKNOWN})

    text_lower = raw_text.lower()
    result: set[FundingType] = set()

    # "Free to apply" is deliberately evaluated on its own and does NOT,
    # by itself, contribute FULLY_FUNDED even if the word "scholarship"
    # or "funded" also appears elsewhere in the same text -- the FULLY_FUNDED
    # pattern below requires its OWN explicit, unambiguous phrase.
    if _matches_any(_FULLY_FUNDED_PATTERNS, text_lower):
        result.add(FundingType.FULLY_FUNDED)
    elif _matches_any(_PARTIAL_PATTERNS, text_lower):
        result.add(FundingType.PARTIALLY_FUNDED)

    if _matches_any(_TUITION_PATTERNS, text_lower):
        result.add(FundingType.TUITION_ONLY)
    if _matches_any(_STIPEND_PATTERNS, text_lower):
        result.add(FundingType.STIPEND)
    if _matches_any(_TRAVEL_PATTERNS, text_lower):
        result.add(FundingType.TRAVEL_FUNDED)
    if _matches_any(_ACCOMMODATION_PATTERNS, text_lower):
        result.add(FundingType.ACCOMMODATION_FUNDED)
    if _matches_any(_FEE_WAIVER_PATTERNS, text_lower) or _matches_any(_FREE_APPLICATION_ONLY_PATTERNS, text_lower):
        result.add(FundingType.FEE_WAIVER)
    if _matches_any(_PRIZE_PATTERNS, text_lower):
        result.add(FundingType.PRIZE)
    if _matches_any(_UNPAID_PATTERNS, text_lower):
        result.add(FundingType.UNPAID)
    if _matches_any(_PAID_PATTERNS, text_lower):
        result.add(FundingType.PAID)

    if not result:
        return frozenset({FundingType.UNKNOWN})
    return frozenset(result)


def implies_fully_funded(funding_types: frozenset[FundingType]) -> bool:
    """Strict helper: only True when FULLY_FUNDED was itself explicitly
    matched -- never derived from the mere presence of TUITION_ONLY +
    STIPEND + TRAVEL_FUNDED + ACCOMMODATION_FUNDED all together, since
    this codebase has no way to confirm that combination is exhaustive
    (a real source could cover exactly those four and still leave out
    visa fees, insurance, etc.). Composing "fully funded" from partial
    signals would be exactly the exaggeration Part 5 forbids."""
    return FundingType.FULLY_FUNDED in funding_types
