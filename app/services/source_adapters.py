"""Source Adapter Layer (Deliverable 5, Feature 1 / Global Architecture
Requirement D).

STEP 1 INVESTIGATION: before this module, `app/workers
/aggregator_tasks.py::_poll_source` branched directly on
`source.source_kind == "telegram_channel"` vs. an `else` (Facebook)
case, calling `fetch_new_telegram_posts`/`fetch_new_facebook_posts`
inline -- fine for exactly two source kinds, but every future source
type (an RSS feed, a government portal, a scholarship database API)
would mean growing that same if/else chain inside the core round-loop
function, mixing planning/looping logic with per-provider fetch
mechanics. This module extracts a minimal, stable adapter boundary so
`_poll_source` calls THROUGH an interface instead of branching itself.

STEP 2 SCOPE, DISCLOSED (original, pre-Phase-3B): exactly two real
adapters existed here, wrapping the two REAL, EXISTING, UNMODIFIED fetch
functions (`aggregator_service.fetch_new_telegram_posts`/
`fetch_new_facebook_posts` -- both still MOCKED per that module's own
disclosed scope). What this module DOES provide is the boundary itself:
`SourceAdapter` (a `Protocol`, structurally typed -- no base-class
inheritance required) plus `ADAPTERS`, a small registry keyed by
`source_kind`. Adding a genuine new source kind means writing one new
adapter class and one new registry entry -- `_poll_source` itself needs
no further changes.

PHASE 3B UPDATE: exactly ONE genuinely LIVE adapter has been added --
`LiveTelegramSourceAdapter`, registered under the distinct kind
`"telegram_channel_live"` (see that class's own docstring for exactly
which channel(s) are routed to it, and `app/services
/telegram_live_client.py` for the real, bounded fetch/parse/retry
implementation and this phase's own honest disclosure of what "live"
does and does not mean in the sandbox this was built in). The two
pre-existing MOCKED adapters (`TelegramSourceAdapter`/
`FacebookSourceAdapter`) are completely UNCHANGED -- every source not
explicitly opted into the live kind keeps today's exact mocked
behavior. A real Facebook Graph API adapter
(`app/services/facebook_graph_client.py`) was also built this phase.
POST-AUDIT UPDATE: it is now registered here as
`FacebookGraphAPISourceAdapter` under the distinct kind
`"facebook_page_live"` (see that class's own docstring) -- resolving
final-audit FINDING-MED-1. No configured page in
`app/data/opportunity_sources.json` is marked `live: true` for
Facebook yet (no real Graph API credential exists in this
environment), so this remains registered-but-dormant exactly like an
un-flipped Telegram channel; see that module's own docstring for the
credential-gating detail.

Existing behavior for the two pre-existing adapters is unchanged: both
still call the same real fetch functions with the same real config
objects and return the same real raw post text, just wrapped in
`RawDiscoveryResult` for a uniform return shape across source kinds
(a lightweight normalization step, not a new data model -- no new
required persistence beyond what
`_poll_source` already used inline).
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import logging
from typing import Protocol

from app.services.aggregator_service import (
    FacebookPageConfig,
    TelegramChannelConfig,
    fetch_new_facebook_posts,
    fetch_new_telegram_posts,
)
from app.services.telegram_live_client import fetch_telegram_channel_live
from app.services.facebook_graph_client import fetch_facebook_page_posts_live

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class RawDiscoveryResult:
    """One normalized raw discovery result -- untrusted external
    content (Security / Prompt Injection Boundary, per this
    deliverable's own spec): `raw_text` is DATA to be extracted from by
    the existing AI extraction pipeline, never instructions, and this
    dataclass carries no field that could be mistaken for one.

    Phase 3B addition: four OPTIONAL provenance/temporal fields, all
    defaulting to `None` so the two pre-existing, still-mocked adapters
    (`TelegramSourceAdapter`/`FacebookSourceAdapter`) construct this
    dataclass exactly as before -- unchanged behavior, unchanged tests.
    A REAL adapter (`LiveTelegramSourceAdapter`) populates them from
    genuine source-provided signals only -- never fabricated. See
    `app.schemas.opportunity.OpportunityCreate`'s own matching fields
    for where these end up persisted (via the existing, unmodified
    `raw_extracted_data` JSONB catch-all -- no migration needed)."""

    raw_text: str
    source_kind: str
    external_item_id: str | None = None
    item_url: str | None = None
    published_at: dt.datetime | None = None
    retrieved_at: dt.datetime | None = None


class SourceAdapter(Protocol):
    """Minimal, stable fetch interface every source kind implements.
    Deliberately narrow -- one method, real config in, real normalized
    results out -- so the core round loop
    (`aggregator_tasks._run_adaptive_discovery_rounds`/`_poll_source`)
    never needs to know which concrete source kind it is polling."""

    def fetch(self, config: object) -> list[RawDiscoveryResult]:
        ...


class TelegramSourceAdapter:
    """Wraps the real, existing, UNMODIFIED
    `fetch_new_telegram_posts` -- unchanged behavior, just normalized
    into `RawDiscoveryResult`."""

    source_kind = "telegram_channel"

    def fetch(self, config: TelegramChannelConfig) -> list[RawDiscoveryResult]:
        return [
            RawDiscoveryResult(raw_text=text, source_kind=self.source_kind)
            for text in fetch_new_telegram_posts(config)
        ]


class LiveTelegramSourceAdapter:
    """Phase 3B: the REAL adapter -- wraps
    `telegram_live_client.fetch_telegram_channel_live`, a genuine,
    bounded, retried, rate-limit-aware fetch of a channel's real public
    `t.me/s/<handle>` preview page. Registered under a DISTINCT
    `source_kind` (`"telegram_channel_live"`) from the pre-existing,
    still-mocked `"telegram_channel"` -- only a `TelegramChannelConfig`
    explicitly marked `live=True` in `app/data/opportunity_sources.json`
    is ever routed here (see `aggregator_tasks._build_source_candidates`);
    every other configured Telegram channel is completely unaffected by
    this class's existence.

    `retrieved_at` is stamped as real "now" at the moment this adapter
    ran -- distinct from each message's own `published_at` (the
    channel's own claimed post time), matching Phase 1's
    `temporal_evidence` module's own `published_at`/`retrieved_at`
    semantics."""

    source_kind = "telegram_channel_live"

    def fetch(self, config: TelegramChannelConfig) -> list[RawDiscoveryResult]:
        retrieved_at = dt.datetime.now(dt.timezone.utc)
        messages = fetch_telegram_channel_live(config.handle)
        return [
            RawDiscoveryResult(
                raw_text=message.raw_text,
                source_kind=self.source_kind,
                external_item_id=message.external_item_id,
                item_url=message.item_url,
                published_at=message.published_at,
                retrieved_at=retrieved_at,
            )
            for message in messages
        ]


class FacebookSourceAdapter:
    """Wraps the real, existing, UNMODIFIED
    `fetch_new_facebook_posts` -- unchanged behavior, just normalized
    into `RawDiscoveryResult`."""

    source_kind = "facebook_page"

    def fetch(self, config: FacebookPageConfig) -> list[RawDiscoveryResult]:
        return [
            RawDiscoveryResult(raw_text=text, source_kind=self.source_kind)
            for text in fetch_new_facebook_posts(config)
        ]


class FacebookGraphAPISourceAdapter:
    """Post-final-audit remediation of FINDING-MED-1: wires the real,
    already-existing `app.services.facebook_graph_client` (built in
    Phase 3B but never registered here) into this same adapter
    boundary, mirroring exactly how `LiveTelegramSourceAdapter` sits
    alongside `TelegramSourceAdapter`.

    Registered under a DISTINCT `source_kind`
    (`"facebook_page_live"`) from the pre-existing, still-mocked
    `"facebook_page"` -- only a `FacebookPageConfig` explicitly marked
    `live=True` in `app/data/opportunity_sources.json` is ever routed
    here (see `aggregator_tasks._build_source_candidates`); every other
    configured Facebook page is completely unaffected by this class's
    existence.

    This class performs NO network access itself and NO fallback --
    it is a thin, one-method wrapper. All real HTTP behavior (the
    official Graph API request, credential gating, retry/timeout
    bounds, error classification) lives entirely in
    `facebook_graph_client.py`, unmodified by this remediation. If that
    module is not configured (`is_configured()` is False -- no real
    token), `fetch_facebook_page_posts_live` raises
    `SourceNotConfiguredError` immediately, with no network call at
    all; this adapter does not catch it -- exactly like
    `LiveTelegramSourceAdapter`, the generic `except Exception:` in
    `aggregator_tasks._poll_source` is what isolates that failure to
    this one source, records an honest reason, and lets discovery
    continue with every other source unaffected. There is no scraping
    fallback anywhere in this path: an unconfigured or failing Graph
    API call never falls back to HTML scraping or to the older mocked
    `FacebookSourceAdapter`.

    `retrieved_at` is stamped as real "now" at the moment this adapter
    ran -- distinct from each post's own `published_at` (Graph API's
    real `created_time`), matching `LiveTelegramSourceAdapter`'s own
    provenance semantics."""

    source_kind = "facebook_page_live"

    def fetch(self, config: FacebookPageConfig) -> list[RawDiscoveryResult]:
        retrieved_at = dt.datetime.now(dt.timezone.utc)
        posts = fetch_facebook_page_posts_live(config)
        return [
            RawDiscoveryResult(
                raw_text=post["raw_text"],
                source_kind=self.source_kind,
                external_item_id=post.get("external_item_id"),
                item_url=post.get("item_url"),
                published_at=post.get("published_at"),
                retrieved_at=retrieved_at,
            )
            for post in posts
        ]


# Registry keyed by the same `source_kind` strings
# `DiscoverySourceCandidate.source_kind` already uses
# (`research_orchestrator.py`). Adding a genuine new source kind is one
# new adapter class + one new entry here.
ADAPTERS: dict[str, SourceAdapter] = {
    "telegram_channel": TelegramSourceAdapter(),
    "telegram_channel_live": LiveTelegramSourceAdapter(),
    "facebook_page": FacebookSourceAdapter(),
    "facebook_page_live": FacebookGraphAPISourceAdapter(),
}


class UnsupportedSourceKindError(Exception):
    """Raised when `get_adapter` is asked for a `source_kind` with no
    registered adapter -- a real, honest failure (Step 7: "unsupported
    source kind" is an explicit test case, not a silent no-op)."""


def get_adapter(source_kind: str) -> SourceAdapter:
    adapter = ADAPTERS.get(source_kind)
    if adapter is None:
        raise UnsupportedSourceKindError(f"no source adapter registered for source_kind={source_kind!r}")
    return adapter
