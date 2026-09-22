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

import json
import logging
import re
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
