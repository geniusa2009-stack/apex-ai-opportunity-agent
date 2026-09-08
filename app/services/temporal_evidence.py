"""Phase 1: Evidence Temporal Fidelity.

Centralizes ALL parsing/derivation of real-world timestamps that feed
`evidence_verification.record_evidence`'s `published_at`/`retrieved_at`
parameters -- so no call site invents its own ad-hoc date parsing, and
there is exactly ONE place a future engineer needs to touch to plug in
a new, real timestamp source.

TIMESTAMP SEMANTICS (constitution for this phase -- see
`app/services/evidence_verification.py`'s own module docstring for how
these two fields are actually consumed by `_is_current`):

- `published_at` = the time the SOURCE itself claims the information was
  published/last updated. This module NEVER fabricates one: it is never
  derived from `retrieved_at`, never derived from `Opportunity.created_at`
  /`updated_at` (those record when APEX's own pipeline touched the row,
  not when the source published anything), and never guessed from an
  LLM's own free-text output. AUDITED FINDING (this phase): as of this
  commit, NO real source anywhere in this codebase's pipeline
  (`app/scraper`, `app/services/aggregator_service.py`'s Gemini
  extraction, or the DuckDuckGo search results D7 classifies) ever
  supplies a genuine source-claimed publication date. `published_at`
  therefore stays `None` for effectively all evidence today -- an honest
  finding about this repo's current data sources, not a permanent
  limitation of this module. If a future source DOES expose a real
  publication date (e.g. an RSS `<pubDate>`, a DDG `source="news"`
  result's `date` field, a scraped page's own "Posted on" text), plug its
  extraction in here, next to `extract_retrieved_at_from_raw_scrape`
  below, as a dedicated `extract_published_at_from_...` function --
  never inline the parsing at the call site.

- `retrieved_at` = the time APEX actually retrieved the evidence.
  `evidence_verification.record_evidence` already defaults this honestly
  to real "now" when not passed explicitly -- that default is correct
  and is NOT changed by this module. This module's only job for
  `retrieved_at` is to supply a MORE PRECISE, still-real alternative when
  one genuinely exists: `ScrapedOpportunityItem.scraped_at` (set by the
  spider at the actual moment of scraping, stored verbatim into
  `Opportunity.raw_extracted_data`) is closer to the true retrieval
  moment than "whenever the D6 pipeline batch job happens to run later."

PRECEDENCE (for `retrieved_at`): a real, parseable, source-adjacent
timestamp (e.g. `scraped_at`) beats the evidence-creation-time `now()`
default. When no such value is present or it fails to parse, this
module returns `None` and the caller passes that through unchanged --
`record_evidence`'s own `retrieved_at or now()` fallback applies exactly
as before this phase.

ANTI-FABRICATION / ANTI-INJECTION POSTURE: `parse_source_timestamp` is a
STRICT ISO 8601 parser only -- mirrors the exact idiom this codebase
already uses in `app/scraper/pipelines.py::PostgresPipeline._parse_deadline`
and `app/workers/tasks.py::_parse_deadline`
(`datetime.fromisoformat(raw.replace("Z", "+00:00"))`), never a fuzzy/
regex "find something that looks like a date" scan. It must ONLY ever be
handed a value that is ALREADY a dedicated, structured timestamp field
(like `scraped_at`) -- never free text such as `evidence_text`/a DDG
snippet/quote. Handing it prose (which may contain attacker-controlled
text, e.g. from a scraped page or a search result) is out of scope by
construction: this function performs no substring/regex date-hunting
inside a larger string, so even a well-formed ISO date embedded in a
sentence is not opportunistically extracted, and free-text instructions
("ignore previous instructions...", "today", "recently") simply fail to
parse and return `None`. It never raises on bad input.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

logger = logging.getLogger(__name__)


def parse_source_timestamp(raw: str | None) -> dt.datetime | None:
    """Strict ISO 8601 parse of an already-isolated timestamp value.

    Returns `None` (never raises) for anything empty, non-string,
    malformed, or ambiguous -- ISO 8601 only, same idiom as this
    codebase's existing `_parse_deadline` helpers. Deliberately does NOT
    attempt to find a date inside a longer string: callers must already
    have isolated a dedicated timestamp field before calling this (see
    module docstring's anti-injection posture)."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        logger.info("temporal_evidence: could not parse %r as an ISO 8601 timestamp -- discarding", raw)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def extract_retrieved_at_from_raw_scrape(raw_extracted_data: Any) -> dt.datetime | None:
    """Best-effort, honest `retrieved_at` candidate for Scrapy-origin
    evidence: `raw_extracted_data["scraped_at"]`, parsed strictly.

    Returns `None` (never raises, never guesses) when `raw_extracted_data`
    isn't a dict, has no `scraped_at`, or `scraped_at` doesn't parse as a
    real ISO 8601 timestamp -- in every such case the caller passes
    `None` on to `record_evidence`, which then applies its own existing,
    correct `now()` default (this function never invents a fallback of
    its own; see module docstring's precedence rule)."""
    if not isinstance(raw_extracted_data, dict):
        return None
    return parse_source_timestamp(raw_extracted_data.get("scraped_at"))
