"""Application Intelligence step, Part C: a deterministic application
requirements projection.

AUDITED FIRST (Part 0): D6's own `ALLOWED_CLAIM_TYPES`
(`app/services/evidence_verification.py`) covers title/deadline/
eligibility_criteria/funding_details/is_legitimate/confidence_score/
min_age/max_age/target_gender/target_academic_levels/country/
application_url -- there is no "required documents" claim type, and no
structured requirements data anywhere in this repository. Inventing one
(a new Claim type, a new LLM extraction call) was judged out of scope
for this phase -- it would mean either fabricating structured data this
codebase's verification pipeline was never asked to produce, or letting
an LLM decide requirements, both of which this phase's own Part C/6
explicitly forbid ("Never invent application requirements" / "Do not let
an LLM invent a requirement").

What DOES already exist, and is genuinely verified: `Opportunity.
eligibility_criteria`, `Opportunity.funding_details`, and `Opportunity.
description` are free text that went through this platform's existing
D6/D7 verification pipeline (or were entered directly by a moderator --
either way, the SAME already-trusted fields every other feature in this
codebase reads). This module does deterministic keyword matching over
those three fields ONLY -- no LLM call, no invented categories -- and
every detected requirement carries the literal matched sentence as
`evidence_snippet` plus which field it came from, so a user (or a future
reviewer) can always verify the claim against the opportunity's own real
text. If none of the three fields mention a document category at all,
that category is simply absent from the result -- never backfilled with
a generic "you'll probably need a CV" guess.

`status` (REQUIRED / CONDITIONAL) is only ever assigned when the
matched sentence itself carries the corresponding language (e.g. "may
be required", "if applicable" -> CONDITIONAL); the default for a bare
mention with no hedging language is REQUIRED, since that is what plain,
unhedged "a CV is required" language actually means. OPTIONAL is
assigned only for explicit optional-marking language ("optional",
"not required but recommended"). Nothing here is a confidence score --
this is a keyword match or it is not (Part 9's own "never expose
arbitrary AI confidence scores" applies equally to this deterministic
layer: no invented certainty number is attached to any of this).
"""

from __future__ import annotations

import dataclasses
import re

from app.models.opportunity import Opportunity

REQUIRED = "required"
CONDITIONAL = "conditional"
OPTIONAL = "optional"

_CONDITIONAL_HINTS = re.compile(
    r"\bmay be required\b|\bif applicable\b|\bwhere applicable\b|\bif requested\b|\bdepending on\b",
    re.IGNORECASE,
)
_OPTIONAL_HINTS = re.compile(
    r"\boptional\b|\bnot required\b|\bnice to have\b|\brecommended but not required\b",
    re.IGNORECASE,
)

# Deliberately small, explicit keyword sets per category -- same
# "extend one line at a time, not a machine-translation table" posture
# as `deadline_intelligence.py`'s own pattern lists. Each category's
# `label` is the user-facing name; `key` is the stable slug persisted in
# `ApplicationRequirementCompletion.requirement_key`.
_CATEGORIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("cv_resume", "CV / Resume", (r"\bcv\b", r"\bcurriculum vitae\b", r"\bresum[eé]\b")),
    ("personal_statement", "Personal Statement", (r"\bpersonal statement\b", r"\bstatement of purpose\b", r"\bmotivation letter\b")),
    ("essay", "Essay", (r"\bessay(s)?\b",)),
    ("recommendation_letter", "Recommendation Letter", (r"\brecommendation letter", r"\bletter(s)? of recommendation\b", r"\breference letter", r"\bLoR\b")),
    ("transcript", "Academic Transcript", (r"\btranscript(s)?\b", r"\bacademic record\b")),
    ("portfolio", "Portfolio", (r"\bportfolio\b", r"\bwork sample")),
    ("language_test", "Language Proficiency Proof", (r"\bIELTS\b", r"\bTOEFL\b", r"\blanguage proficiency\b", r"\blanguage test\b")),
    ("test_score", "Standardized Test Score", (r"\bSAT\b", r"\bGRE\b", r"\bGMAT\b", r"\btest score")),
    ("financial_document", "Financial Document", (r"\bfinancial (?:statement|document|proof)", r"\bbank statement\b", r"\bproof of funds\b")),
    ("passport_travel_document", "Passport / Travel Document", (r"\bpassport\b", r"\bvisa\b", r"\btravel document\b")),
)

# `description` last -- it's the least structured of the three fields,
# so a match there is still real evidence, just the lowest-priority
# source when a category matches in more than one field (the snippet
# shown is always the FIRST matching field's own sentence).
_SOURCE_FIELDS: tuple[str, ...] = ("eligibility_criteria", "funding_details", "description")

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


@dataclasses.dataclass
class RequirementItem:
    key: str
    label: str
    status: str  # REQUIRED | CONDITIONAL | OPTIONAL
    source_field: str
    evidence_snippet: str


def _sentence_containing(text: str, pattern: re.Pattern[str]) -> str | None:
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        if pattern.search(sentence):
            return sentence.strip()
    return None


def _classify_status(snippet: str) -> str:
    if _OPTIONAL_HINTS.search(snippet):
        return OPTIONAL
    if _CONDITIONAL_HINTS.search(snippet):
        return CONDITIONAL
    return REQUIRED


def extract_requirements(opportunity: Opportunity) -> list[RequirementItem]:
    """Deterministic, evidence-traceable requirement extraction from an
    opportunity's own already-verified free-text fields. Pure function --
    no DB access, no network call, no LLM. Returns an empty list (never
    a fabricated default set) when none of the three source fields
    mention any known category."""
    items: list[RequirementItem] = []
    seen_keys: set[str] = set()

    for key, label, patterns in _CATEGORIES:
        for field_name in _SOURCE_FIELDS:
            text = getattr(opportunity, field_name, None)
            if not text:
                continue
            for pattern_str in patterns:
                pattern = re.compile(pattern_str, re.IGNORECASE)
                snippet = _sentence_containing(text, pattern)
                if snippet is not None:
                    items.append(
                        RequirementItem(
                            key=key, label=label, status=_classify_status(snippet),
                            source_field=field_name, evidence_snippet=snippet,
                        )
                    )
                    seen_keys.add(key)
                    break
            if key in seen_keys:
                break

    return items
