"""Canonical taxonomy normalization (Deliverable 5, Feature 4 / Global
Architecture Requirement C).

STEP 1 INVESTIGATION: `app/models/enums.py::OpportunityType` is the
one, real, existing source of truth for "what kind of opportunity is
this" in this codebase -- exactly FOUR values today (`SCHOLARSHIP`,
`INTERNSHIP`, `VOLUNTEER`, `COURSE`; confirmed by direct read of that
enum). D5 does NOT expand this taxonomy -- adding a dozen new enum
values to match the D5 spec's own aspirational "eventually supports"
list would be a real, unvalidated product/schema decision this
deliverable has no authority to make, and every new value would need to
flow correctly through every existing filter/schema/frontend that
assumes today's four. What WAS genuinely missing before this module: a
single, centralized place that maps free-text/multilingual category
labels ("scholarship", "scholarships", "funded scholarship", "منحة",
"bourse", ...) onto one of the four REAL canonical values. Before this
module, that logic was duplicated/fragile in exactly one place --
`app/workers/aggregator_tasks.py::_TYPE_CATEGORY_HINTS`, a hardcoded,
English-only, substring-match dict with no formal link to
`OpportunityType` at all. This module replaces that dict; nothing else
in this codebase had its own competing category-matching logic (D1's
`compute_opportunity_coverage` already keys directly off the real
`OpportunityType` enum values, never off free text, so it needed no
change -- see that function's own docstring).

`CATEGORY_ALIASES` is intentionally small and honest: common English
synonyms/plurals plus a handful of real Arabic and French synonyms for
the FOUR existing types (this codebase's real markets/sources include
Arabic- and French-language content -- see `app/services
/cultural_localizer.py` and the Telegram/Facebook source config for
precedent). It is NOT a machine-translation table and does not attempt
every one of the ~15 languages the D5 spec lists architecturally --
extending it is a one-line dict addition per new alias, never a
structural change, which is the real, disclosed scope this deliverable
commits to (see this module's own tests for the exact aliases covered).

Geography ("Global Architecture Requirement A"): `Opportunity.country`
(app/models/opportunity.py) is a free-text, nullable `String(120)` with
no canonical country-code system anywhere in this codebase (confirmed:
zero `pycountry`/`babel`/ISO-3166 dependency in `requirements.txt`).
Building a full ISO-3166 database from scratch, or adding a new pinned
third-party dependency, is real overbuilding risk for a system whose real, vetted source content
(Phase 3A/3B's own vetting trail) names a genuinely broader, but still
bounded, real set of countries -- not a full global range. `COUNTRY_ALIASES`
below is therefore a small,
explicit, extensible static table -- NOT a full country database --
covering what this codebase's real data/sources actually use today,
with a documented, one-line-per-country extension point. This resolver
never touches the stored `Opportunity.country` value/format (still free
text, unchanged, no migration) -- it is a planning-signal helper for
discovery/coverage/graph code, not a schema change.
"""
from __future__ import annotations

from app.models.enums import OpportunityType


def _normalize_key(text: str) -> str:
    """Deterministic, no-LLM normalization: casefold + strip + collapse
    internal whitespace. Same posture as
    `research_trajectory_memory.normalize_objective`."""
    return " ".join(text.strip().casefold().split())


# Canonical opportunity-category aliases -> one of the FOUR real,
# existing `OpportunityType` values. Keys are pre-normalized via
# `_normalize_key` (casefolded, whitespace-collapsed) so lookups never
# depend on the caller's own casing/spacing.
CATEGORY_ALIASES: dict[str, OpportunityType] = {
    # SCHOLARSHIP
    "scholarship": OpportunityType.SCHOLARSHIP,
    "scholarships": OpportunityType.SCHOLARSHIP,
    "funded scholarship": OpportunityType.SCHOLARSHIP,
    "fully funded scholarship": OpportunityType.SCHOLARSHIP,
    "scholar": OpportunityType.SCHOLARSHIP,
    "grant": OpportunityType.SCHOLARSHIP,
    "funding": OpportunityType.SCHOLARSHIP,
    "منحة": OpportunityType.SCHOLARSHIP,
    "منحة دراسية": OpportunityType.SCHOLARSHIP,
    "منح": OpportunityType.SCHOLARSHIP,
    "bourse": OpportunityType.SCHOLARSHIP,
    "bourse d'études": OpportunityType.SCHOLARSHIP,
    "bourses": OpportunityType.SCHOLARSHIP,
    # INTERNSHIP
    "internship": OpportunityType.INTERNSHIP,
    "internships": OpportunityType.INTERNSHIP,
    "internships_training": OpportunityType.INTERNSHIP,
    "intern": OpportunityType.INTERNSHIP,
    "training": OpportunityType.INTERNSHIP,
    "traineeship": OpportunityType.INTERNSHIP,
    "تدريب": OpportunityType.INTERNSHIP,
    "تدريب صيفي": OpportunityType.INTERNSHIP,
    "stage": OpportunityType.INTERNSHIP,
    "stage professionnel": OpportunityType.INTERNSHIP,
    # COURSE
    "course": OpportunityType.COURSE,
    "courses": OpportunityType.COURSE,
    "self_development": OpportunityType.COURSE,
    "self development": OpportunityType.COURSE,
    "education": OpportunityType.COURSE,
    "educ": OpportunityType.COURSE,
    "training course": OpportunityType.COURSE,
    "online course": OpportunityType.COURSE,
    "دورة": OpportunityType.COURSE,
    "دورة تدريبية": OpportunityType.COURSE,
    "cours": OpportunityType.COURSE,
    "formation": OpportunityType.COURSE,
    # VOLUNTEER
    "volunteer": OpportunityType.VOLUNTEER,
    "volunteering": OpportunityType.VOLUNTEER,
    "volunteers": OpportunityType.VOLUNTEER,
    "تطوع": OpportunityType.VOLUNTEER,
    "عمل تطوعي": OpportunityType.VOLUNTEER,
    "bénévolat": OpportunityType.VOLUNTEER,
    "volontariat": OpportunityType.VOLUNTEER,
}


def normalize_category(text: str | None) -> OpportunityType | None:
    """Maps free-text/multilingual category label `text` onto one of
    the four real `OpportunityType` values, or `None` when unrecognized.
    Exact normalized-key lookup only (Step 1's own "deterministic, no
    LLM" posture) -- deliberately no fuzzy/substring matching, which is
    exactly the fragile behavior `_TYPE_CATEGORY_HINTS` had and this
    module replaces. `None` is a real, honest outcome for a genuinely
    unknown category, never a fallback guess."""
    if not text:
        return None
    return CATEGORY_ALIASES.get(_normalize_key(text))


# Small, explicit, extensible geography resolver -- see module docstring
# for why this is NOT a full ISO-3166 database. Values are ISO-3166-1
# alpha-2 codes where a real country is meant, or the documented
# "GLOBAL" sentinel for online/worldwide/no-single-country opportunities
# (this codebase's own existing "Global" fallback value, see
# `app/data/opportunity_sources.json` / dummy data).
COUNTRY_ALIASES: dict[str, str] = {
    "egypt": "EG",
    "eg": "EG",
    "مصر": "EG",
    "جمهورية مصر العربية": "EG",
    "égypte": "EG",
    "egypte": "EG",
    "ägypten": "EG",
    "agypten": "EG",
    "global": "GLOBAL",
    "worldwide": "GLOBAL",
    "online": "GLOBAL",
    "remote": "GLOBAL",
    "international": "GLOBAL",
    "عالمي": "GLOBAL",
    "دولي": "GLOBAL",
    # D5.1 (Global Country Extraction Unblock): the upstream AI
    # extraction layer (`aggregator_service.process_raw_post_via_ai`)
    # can now emit a real free-text `country_hint` other than
    # "Egypt"/"Global" (see that module's own docstring). These entries
    # are the exact, minimal set this deliverable's own multilingual
    # test cases require -- English + real Arabic/French synonyms for
    # France and Kenya (the two languages/countries the spec's own
    # behavioral test cases name), plus the three other countries named
    # in the spec's example list (Germany, United States, United
    # Kingdom) in English only, since no test case requires their
    # Arabic/French synonyms. Same one-line-per-alias extension
    # posture as the Egypt/Global entries above -- not a country
    # database, still a small explicit static table.
    "france": "FR",
    "fr": "FR",
    "فرنسا": "FR",
    "kenya": "KE",
    "ke": "KE",
    "كينيا": "KE",
    "germany": "DE",
    "allemagne": "DE",
    "united states": "US",
    "united states of america": "US",
    "usa": "US",
    "us": "US",
    "united kingdom": "GB",
    "uk": "GB",
    "great britain": "GB",
    # Phase 4 (Taxonomy / Geography Scalability): these entries are NOT
    # a speculative expansion -- each country below is one this
    # codebase's own real, vetted source-vetting trail already named as
    # real content genuinely observed at a real, live source: Phase 3A's
    # own vetting notes for `@opcorners` ("recent posts spanning the US,
    # China, UK, Germany, Saudi Arabia, Hong Kong, South Korea") and
    # `@FullOpportunities` ("UK, Finland, Hungary, Germany, Romania,
    # Japan, Singapore, Australia, Georgia"), plus Phase 3B.1's own real
    # live-fetched `@opcorners` sample text ("Youth Forum on UN Global
    # Goals 2026 Switzerland"). English forms only, except where an
    # Arabic form is one this codebase already has clear, safe
    # precedent for (mirroring the Egypt/France/Kenya entries above) --
    # no machine-translated or guessed multilingual form was added.
    "china": "CN",
    "الصين": "CN",
    "saudi arabia": "SA",
    "ksa": "SA",
    "السعودية": "SA",
    "المملكة العربية السعودية": "SA",
    "hong kong": "HK",
    # "korea" alone is deliberately NOT mapped -- North/South Korea are
    # both real, distinct ISO-3166 entries and this codebase has no
    # real signal to disambiguate bare "Korea" honestly; only the
    # unambiguous, actually-observed "South Korea" form is mapped.
    "south korea": "KR",
    "republic of korea": "KR",
    "finland": "FI",
    "hungary": "HU",
    "romania": "RO",
    "japan": "JP",
    "اليابان": "JP",
    "singapore": "SG",
    "australia": "AU",
    "switzerland": "CH",
    "سويسرا": "CH",
    # KNOWN, DISCLOSED AMBIGUITY: "Georgia" the country (ISO "GE") and
    # Georgia the U.S. state share the identical English name with no
    # reliable disambiguating signal in free text alone. Mapped to the
    # country per this codebase's actual vetting context (an
    # international scholarship-source vetting note, not a US-state
    # context), but this is a real, disclosed limitation of a plain
    # string-keyed table, not a resolved edge case -- a future,
    # context-aware resolver would be needed to truly disambiguate this.
    "georgia": "GE",
}


def normalize_country(text: str | None) -> str | None:
    """Maps free-text country/geography label `text` onto an ISO-3166
    alpha-2 code or the `"GLOBAL"` sentinel, or `None` when unrecognized.
    Does NOT modify `Opportunity.country`'s own stored value -- this is
    a planning-signal helper only (see module docstring)."""
    if not text:
        return None
    return COUNTRY_ALIASES.get(_normalize_key(text))
