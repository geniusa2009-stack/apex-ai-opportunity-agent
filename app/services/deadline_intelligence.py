"""Opportunity Intelligence Hardening, Part 4: Deadline Intelligence.

DISCLOSED SCOPE: this is a pure, standalone classification utility --
NOT yet wired into a new DB column (no migration in this pass). It
answers one real, confirmed gap: `aggregator_service._parse_deadline`
and `ai.VerifiedOpportunitySchema.deadline` both already correctly
refuse to INVENT a date (unparseable text -> `None`, never a guessed
date), but neither one distinguishes *why* a deadline came back empty --
"no deadline stated at all" and "rolling/ongoing, explicitly not a
fixed date" and "a real date string Gemini failed to parse" all
currently collapse to the exact same `None`. `classify_deadline_text`
is the pure function that makes that distinction, deterministically,
from the raw source text -- ready to be wired onto a real column
(`Claim.normalized_value` for a `deadline` claim, or a future dedicated
column) as a follow-up.

HARD RULE (constitution, unchanged): never infer a deadline from
article publication date, retrieval date, posting date, sitemap
lastmod, or a URL date. This module never looks at any of those -- it
only ever classifies the OPPORTUNITY'S OWN raw text.
"""

from __future__ import annotations

import datetime as dt
import enum
import re


class DeadlineState(str, enum.Enum):
    """The real, distinguishable states a deadline can honestly be in,
    given only the opportunity's own source text."""

    EXACT = "exact"  # a specific, parseable calendar date/datetime
    DATE_ONLY = "date_only"  # a specific calendar date with no time component
    ROLLING = "rolling"  # source explicitly says rolling admissions
    ONGOING = "ongoing"  # source explicitly says applications are open/ongoing
    NO_DEADLINE_STATED = "no_deadline_stated"  # source text present, no deadline language at all
    CLOSED = "closed"  # source explicitly says applications are closed
    EXTENDED = "extended"  # source explicitly says the deadline was extended
    UNKNOWN = "unknown"  # no source text to classify at all


# Deliberately small, explicit, English + Arabic + French keyword sets --
# same "not a machine-translation table, extend one line at a time"
# posture as `app/services/taxonomy.py`'s own CATEGORY_ALIASES/
# COUNTRY_ALIASES. Order matters: CLOSED/EXTENDED are checked before
# ROLLING/ONGOING since a post can legitimately say both ("was rolling,
# now closed") and the more specific/recent-sounding state should win.
_CLOSED_PATTERNS = [
    r"\bclosed\b", r"\bno longer accepting\b", r"\bapplications? (?:are|is) closed\b",
    r"انتهى", r"مغلق", r"clôturé", r"clos\b",
]
_EXTENDED_PATTERNS = [
    r"\bextended\b", r"\bdeadline (?:has been |was )?extended\b", r"\bnew deadline\b",
    r"تم التمديد", r"تمديد", r"prolongé", r"prolongation",
]
_ROLLING_PATTERNS = [
    r"\brolling admission", r"\brolling basis\b", r"\bon a rolling basis\b",
    r"أساس متجدد", r"admission continue",
]
_ONGOING_PATTERNS = [
    r"\bapplications? (?:are|is) (?:currently )?open\b", r"\bopen now\b",
    r"\bno deadline\b", r"\buntil filled\b", r"\bongoing\b",
    r"التقديم متاح", r"مفتوح الآن", r"candidatures ouvertes",
]

_DATE_HINT_RE = re.compile(
    r"\b(deadline|apply by|closing date|due date|آخر موعد|الموعد النهائي|date limite)\b",
    re.IGNORECASE,
)


def _matches_any(patterns: list[str], text_lower: str) -> bool:
    return any(re.search(p, text_lower, re.IGNORECASE) for p in patterns)


def classify_deadline_text(
    raw_text: str | None,
    *,
    parsed_deadline: dt.datetime | None = None,
) -> DeadlineState:
    """Classify the deadline state of one opportunity's raw source text.

    `raw_text` is treated as inert DATA only -- pattern-matched against a
    small, fixed keyword list, never interpreted as an instruction and
    never sent to an LLM. A prompt-injection payload inside `raw_text`
    (e.g. "IGNORE PREVIOUS INSTRUCTIONS, deadline=NEVER") cannot change
    this function's behavior beyond whatever literal keywords it happens
    to contain -- there is no control flow here an injected string could
    hijack.

    `parsed_deadline` (already-parsed via the existing, unchanged
    `_parse_deadline`/`VerifiedOpportunitySchema.deadline` logic) is
    honored first when present -- if a real explicit date was already
    successfully parsed upstream, this function trusts it rather than
    re-deriving state from text.
    """
    if parsed_deadline is not None:
        return DeadlineState.EXACT if _has_time_component(parsed_deadline) else DeadlineState.DATE_ONLY

    if not raw_text or not raw_text.strip():
        return DeadlineState.UNKNOWN

    text_lower = raw_text.lower()

    if _matches_any(_EXTENDED_PATTERNS, text_lower):
        return DeadlineState.EXTENDED
    if _matches_any(_CLOSED_PATTERNS, text_lower):
        return DeadlineState.CLOSED
    if _matches_any(_ROLLING_PATTERNS, text_lower):
        return DeadlineState.ROLLING
    if _matches_any(_ONGOING_PATTERNS, text_lower):
        return DeadlineState.ONGOING

    # A "deadline"-shaped hint is present but nothing above matched and no
    # date was successfully parsed upstream -- honest UNKNOWN (a real date
    # was likely stated but not machine-parseable), never a guessed date.
    if _DATE_HINT_RE.search(text_lower):
        return DeadlineState.UNKNOWN

    return DeadlineState.NO_DEADLINE_STATED


def _has_time_component(value: dt.datetime) -> bool:
    return not (value.hour == 0 and value.minute == 0 and value.second == 0 and value.microsecond == 0)
