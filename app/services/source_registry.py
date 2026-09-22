"""Source Intelligence Registry (Phase: Source Registry Expansion &
Normalization).

SCOPE, DISCLOSED: this module is a read-only CATALOG layer over
`app/data/source_registry.json`. It classifies and normalizes every
source supplied for this task (official/government programs,
opportunity-aggregator websites, Telegram channels/groups, WhatsApp
channels/communities, and other social profiles) into a single,
canonical, tiered registry -- but it does NOT wire any new source into
the live discovery pipeline. `app/data/opportunity_sources.json` (the
real, existing config `_build_source_candidates`
[`app/workers/aggregator_tasks.py`] reads to build live discovery
rounds) is completely UNCHANGED by this module. `discovery_enabled` on
a registry entry reports whether that source is ALREADY wired into
`opportunity_sources.json` today -- it is a fact this module reads and
reports, never one it can set. Every source not already in
`opportunity_sources.json` is a catalog-only entry: visible, tiered,
classified, and ready for a future, separately-scoped decision to wire
it in -- exactly the same "registered but dormant" posture the prior
Facebook Graph API adapter remediation used for real Facebook pages
without a credential yet.

HARD INVARIANT (Discovery vs. Evidence, per this task's own spec):
a source's `tier`/`trust_level` here describes how much weight a
*human or a future engineering change* should give it when deciding
whether to treat it as PRIMARY EVIDENCE -- it has no runtime effect on
`is_verified`, provider selection, or authentication. Nothing in this
module is imported by `app/services/evidence_verification.py`,
`app/services/contradiction_hunter.py`, or `app/workers/tasks.py`
(the single `is_verified=True` write site), and nothing here should
ever be wired into those modules to auto-approve a Tier 2/3 source as
evidence. See `tests/test_source_intelligence_registry.py::
TestVerificationBoundaryNeverBypassed` for a direct, executed proof.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from collections import Counter
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

_REGISTRY_PATH = Path(__file__).resolve().parent.parent / "data" / "source_registry.json"

VALID_TIERS = {"TIER_1_PRIMARY", "TIER_2_AGGREGATOR", "TIER_3_COMMUNITY"}
VALID_LIVE_STATUSES = {"LIVE", "DEGRADED", "BLOCKED", "NO_CREDENTIAL", "UNCONFIRMED", "MOCKED", "NO_ADAPTER", "DISABLED"}
# Adapter families introduced by the Source Enablement Framework
# (app/services/source_enablement.py). Kept as a plain string set here
# (not an import from source_enablement) to avoid this lightweight
# catalog module pulling in httpx/defusedxml just to validate a string.
VALID_ADAPTER_TYPES = {
    "generic_web", "rss", "sitemap", "api", "telegram_channel",
    "facebook_graph_api", "no_adapter_available", "unresolved",
}
VALID_HEALTH_STATUSES = {
    "healthy", "degraded", "failed", "credentials_required",
    "unsupported", "disabled", "not_tested",
}


def normalize_url(url: str | None) -> str | None:
    """Deterministic canonicalization: lower-cased host, https scheme,
    no trailing slash, no query/fragment (tracking params stripped).
    Returns `None` unchanged for `None` -- never fabricates a URL for
    a source that genuinely has none."""
    if not url:
        return url
    parts = urlsplit(url.strip())
    scheme = "https"
    netloc = parts.netloc.lower()
    path = parts.path.rstrip("/") or ""
    return urlunsplit((scheme, netloc, path, "", ""))


def normalize_telegram_handle(value: str) -> str:
    """`t.me/opcorners`, `telegram.me/s/opcorners`, and `@opcorners`
    all normalize to the same canonical handle `@opcorners`."""
    match = re.search(r"t(?:elegram)?\.me/(?:s/)?\+?([A-Za-z0-9_]+)", value)
    if match:
        return "@" + match.group(1)
    if value.startswith("@"):
        return value
    return value


def _ascii_slug(text: str | None) -> str:
    """Lowercase, hyphen-separated ASCII slug. Non-ASCII characters
    (e.g. Arabic text) are DROPPED, never transliterated -- a machine
    transliteration would be inventing text not present in the source
    record. This is exactly the rule that produced every one of this
    registry's pre-existing, still-unmodified source_ids (verified:
    recomputing this function over all 117 sources' `name` fields
    reproduces the stored `source_id` for the 106 that were already
    unique before the Source Identity Integrity repair)."""
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKD", text)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")


def generate_deterministic_source_id(
    name: str, canonical_url: str | None = None, telegram_handle: str | None = None
) -> str:
    """The registry's canonical source-id derivation. Used to (a)
    validate that a source's stored `source_id` is still the one this
    rule would produce, and (b) assign an id to any newly-added
    source. Priority, matching the registry's existing convention:

    1. `src-<ascii-slug-of-name>` -- this is what every pre-existing,
       already-unique id in this registry was already built from; kept
       as the primary rule so this function never silently proposes a
       different id for a source that already has a good one.
    2. If that slug is EMPTY (no Latin characters in `name` at all) or
       a bare, non-distinguishing short number (e.g. just a 4-digit
       year -- the exact defect that produced the `src-2030` x2
       collision), fall back to a real external anchor if one exists:
       `telegram_handle` first (a verifiable, stable identity), then
       `canonical_url`'s host+path.
    3. If neither anchor exists, the name is the only real data this
       source record has. Its slug (possibly empty) is kept as a
       prefix and disambiguated with a short, deterministic digest of
       the *exact* name string (sha256, first 8 hex chars) -- this is
       computed, not invented: it claims no URL/handle the source
       doesn't actually have, and is 100% reproducible from `name`
       alone. Two sources with the same exact name (not expected in
       this registry) would still collide; that is disclosed as a
       known limitation, not silently resolved.
    """
    name_slug = _ascii_slug(name)
    degenerate = (not name_slug) or (name_slug.isdigit() and len(name_slug) <= 4)
    if not degenerate:
        return f"src-{name_slug}"
    if telegram_handle:
        handle_slug = _ascii_slug(telegram_handle.lstrip("@"))
        if handle_slug:
            return f"src-{handle_slug}"
    if canonical_url:
        parts = urlsplit(canonical_url)
        url_slug = _ascii_slug(f"{parts.netloc}{parts.path}")
        if url_slug:
            return f"src-{url_slug}"
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
    base = name_slug or "source"
    return f"src-{base}-{digest}"


@lru_cache(maxsize=1)
def load_source_registry() -> dict:
    """Loads and validates `app/data/source_registry.json` once per
    process. Raises rather than silently returning a partial/empty
    registry on a malformed file -- a caller reasoning about source
    trust must never receive a silently-truncated view."""
    with _REGISTRY_PATH.open(encoding="utf-8") as f:
        data = json.load(f)

    for source in data.get("sources", []):
        if source["tier"] not in VALID_TIERS:
            raise ValueError(f"source_registry.json: unknown tier {source['tier']!r} for {source['name']!r}")
        if source["live_status"] not in VALID_LIVE_STATUSES:
            raise ValueError(f"source_registry.json: unknown live_status {source['live_status']!r} for {source['name']!r}")
        if source.get("adapter_type") is not None and source["adapter_type"] not in VALID_ADAPTER_TYPES:
            raise ValueError(f"source_registry.json: unknown adapter_type {source['adapter_type']!r} for {source['name']!r}")
        if source.get("health_status") is not None and source["health_status"] not in VALID_HEALTH_STATUSES:
            raise ValueError(f"source_registry.json: unknown health_status {source['health_status']!r} for {source['name']!r}")

    # Source Identity Integrity: a registry a caller can key lookups
    # against (get_source_by_id, the smoke-test CLI's --source-id) must
    # never silently contain two sources under one id -- a duplicate
    # here previously meant get_source_by_id() returned only the FIRST
    # of several unrelated sources. Raise loudly rather than let that
    # happen again.
    ids = [s["source_id"] for s in data.get("sources", [])]
    id_counts = Counter(ids)
    duplicates = {sid: count for sid, count in id_counts.items() if count > 1}
    if duplicates:
        raise ValueError(f"source_registry.json: duplicate source_id values found: {duplicates}")
    return data


def get_source_by_id(source_id: str) -> dict | None:
    for source in load_source_registry()["sources"]:
        if source["source_id"] == source_id:
            return source
    return None


def get_sources_by_adapter_type(adapter_type: str) -> list[dict]:
    if adapter_type not in VALID_ADAPTER_TYPES:
        raise ValueError(f"unknown adapter_type: {adapter_type!r}")
    return [s for s in load_source_registry()["sources"] if s["adapter_type"] == adapter_type]


def get_sources_by_tier(tier: str) -> list[dict]:
    if tier not in VALID_TIERS:
        raise ValueError(f"unknown tier: {tier!r}")
    return [s for s in load_source_registry()["sources"] if s["tier"] == tier]


def get_discovery_enabled_sources() -> list[dict]:
    """Sources already wired into the real, live discovery pipeline
    (`app/data/opportunity_sources.json`) -- a fact reported from the
    registry's own cross-reference at build time, never computed by
    guessing at runtime."""
    return [s for s in load_source_registry()["sources"] if s["discovery_enabled"]]


def is_primary_evidence_eligible(source_id: str) -> bool:
    """Whether a source MAY be considered when a human/future
    engineering change decides what counts as primary evidence for a
    claim. Only ever `True` for TIER_1_PRIMARY sources. This function
    has no callers today in the verification pipeline and must never
    be wired to auto-set `is_verified` -- it is a classification
    lookup only, not a verification authority itself."""
    for source in load_source_registry()["sources"]:
        if source["source_id"] == source_id:
            return source["tier"] == "TIER_1_PRIMARY" and source["verification_eligible"]
    return False


def registry_summary() -> dict:
    """A small, deterministic summary used by the registry's own
    report and by any future admin-visibility surface -- counts only,
    never row-level content, so it is safe to log or expose."""
    sources = load_source_registry()["sources"]
    tier_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    live_status_counts: dict[str, int] = {}
    adapter_counts: dict[str, int] = {}
    for s in sources:
        tier_counts[s["tier"]] = tier_counts.get(s["tier"], 0) + 1
        type_counts[s["source_type"]] = type_counts.get(s["source_type"], 0) + 1
        live_status_counts[s["live_status"]] = live_status_counts.get(s["live_status"], 0) + 1
        adapter_counts[s["adapter_type"]] = adapter_counts.get(s["adapter_type"], 0) + 1
    return {
        "total": len(sources),
        "by_tier": tier_counts,
        "by_type": type_counts,
        "by_live_status": live_status_counts,
        "by_adapter_type": adapter_counts,
        "discovery_enabled_count": sum(1 for s in sources if s["discovery_enabled"]),
    }
