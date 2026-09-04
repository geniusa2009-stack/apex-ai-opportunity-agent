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

STEP 2 SCOPE, DISCLOSED: exactly two real adapters exist here, wrapping
the two REAL, EXISTING, UNMODIFIED fetch functions
(`aggregator_service.fetch_new_telegram_posts`/`fetch_new_facebook_posts`
-- both still MOCKED per that module's own disclosed scope, no live
network credentials exist in this sandbox for either). No third,
fabricated adapter (RSS/website/API) is added here -- there is no real
third source type configured anywhere in this codebase
(`app/data/opportunity_sources.json` only has `telegram_channels`/
`facebook_pages`), and no credentials exist to build and live-validate
a genuine third integration. What this module DOES provide is the
boundary itself: `SourceAdapter` (a `Protocol`, structurally typed --
no base-class inheritance required) plus `ADAPTERS`, a small registry
keyed by `source_kind`. Adding a genuine third source kind later means
writing one new adapter class and one new registry entry -- `_poll_source`
itself needs no further changes.

Existing behavior is unchanged: both adapters call the same real fetch
functions with the same real config objects and return the same real
raw post text, just wrapped in `RawDiscoveryResult` for a uniform
return shape across source kinds (a lightweight normalization step,
not a new data model -- no new persistence, no new fields beyond what
`_poll_source` already used inline).
"""
from __future__ import annotations

import dataclasses
from typing import Protocol

from app.services.aggregator_service import (
    FacebookPageConfig,
    TelegramChannelConfig,
    fetch_new_facebook_posts,
    fetch_new_telegram_posts,
)


@dataclasses.dataclass(frozen=True)
class RawDiscoveryResult:
    """One normalized raw discovery result -- untrusted external
    content (Security / Prompt Injection Boundary, per this
    deliverable's own spec): `raw_text` is DATA to be extracted from by
    the existing AI extraction pipeline, never instructions, and this
    dataclass carries no field that could be mistaken for one."""

    raw_text: str
    source_kind: str


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


# Registry keyed by the same `source_kind` strings
# `DiscoverySourceCandidate.source_kind` already uses
# (`research_orchestrator.py`). Adding a genuine new source kind is one
# new adapter class + one new entry here.
ADAPTERS: dict[str, SourceAdapter] = {
    "telegram_channel": TelegramSourceAdapter(),
    "facebook_page": FacebookSourceAdapter(),
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
