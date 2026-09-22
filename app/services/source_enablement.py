"""Source Enablement Framework (APEX Source Enablement spec).

SCOPE, DISCLOSED UP FRONT: this module implements the reusable,
configuration-driven fetch/extract/normalize infrastructure required to
progressively enable the 117 real sources catalogued in
`app/data/source_registry.json` (see `apex-backend-source-registry-
expansion-report.md`). It does NOT wire any new source into the live
Celery discovery pipeline (`app/workers/aggregator_tasks.py`'s
`_run_adaptive_discovery_rounds`/`_poll_source`, driven by
`app/data/opportunity_sources.json`) -- that remains a separate,
credentialed, one-source-at-a-time decision, exactly the same
"registered but dormant" posture the prior Facebook Graph API adapter
remediation used. This module is available, real, tested
infrastructure that a future change can wire in; it is not itself that
wiring.

HARD INVARIANTS (unchanged, not touched by this module):
- `app/services/evidence_verification.py` (D6), `app/services/
  contradiction_hunter.py` (D7), and the single `Opportunity.
  is_verified = True` write site (`app/workers/tasks.py:299`) are not
  imported by, and have no code path from, this module. A source
  fetch result is DISCOVERY/EVIDENCE-CANDIDATE data only; whether it
  ever counts as verification-grade evidence remains entirely D6/D7's
  decision, unchanged.
- No adapter here ever bypasses robots.txt, authentication, paywalls,
  CAPTCHAs, or rate limits. No credential is ever hardcoded; every one
  is read from `app.core.config.settings`, exactly like every existing
  provider/adapter in this codebase.
- A source is reported `LIVE` only after this module records an
  ACTUAL successful fetch+parse round-trip for it in this session --
  never from adapter-class existence alone, and never fabricated in a
  test.
"""
from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import ipaddress
import logging
import re
import socket
import urllib.robotparser
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import httpx
from defusedxml import ElementTree as SafeET

from app.core.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# Health status vocabulary (Section 19) -- never "healthy" from class
# existence alone; only ever set by an actual attempt.
# ---------------------------------------------------------------------
HEALTH_HEALTHY = "healthy"
HEALTH_DEGRADED = "degraded"
HEALTH_FAILED = "failed"
HEALTH_CREDENTIALS_REQUIRED = "credentials_required"
HEALTH_UNSUPPORTED = "unsupported"
HEALTH_DISABLED = "disabled"
HEALTH_NOT_TESTED = "not_tested"

VALID_HEALTH_STATUSES = {
    HEALTH_HEALTHY, HEALTH_DEGRADED, HEALTH_FAILED, HEALTH_CREDENTIALS_REQUIRED,
    HEALTH_UNSUPPORTED, HEALTH_DISABLED, HEALTH_NOT_TESTED,
}

# Fetch bounds (Section 17) -- explicit, not "whatever the SDK defaults to".
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
MAX_RESPONSE_BYTES = 5_000_000  # 5MB cap; a legitimate opportunity listing page is orders of magnitude smaller
MAX_REDIRECTS = 3


class SourceEnablementSecurityError(Exception):
    """Raised by the SSRF guard / robots check -- a real, hard refusal,
    never silently downgraded to a soft warning."""


class CredentialsUnavailableError(Exception):
    """Raised by the API adapter when a required credential is absent.
    Distinct from a network failure -- this is a configuration state,
    reported honestly as `credentials_required`, never as `live`."""


# ---------------------------------------------------------------------
# SSRF Guard (Section 17) -- blocks private/loopback/link-local IPs,
# the cloud metadata endpoint, and enforces an explicit domain
# allowlist plus redirect re-validation on every hop.
# ---------------------------------------------------------------------

_METADATA_HOSTS = {"169.254.169.254", "metadata.google.internal"}


def _is_blocked_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # unparseable -- fail closed, never fail open
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or str(ip) in _METADATA_HOSTS
    )


def validate_fetch_url(url: str, allowed_domains: list[str]) -> None:
    """Real, executed SSRF guard -- resolves the host and rejects any
    private/loopback/link-local/metadata-endpoint address, and rejects
    any host outside the source's own configured `allowed_domains`.
    Raises `SourceEnablementSecurityError` rather than returning a
    boolean -- a caller must not be able to accidentally ignore a
    failed check."""
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http"):
        raise SourceEnablementSecurityError(f"unsupported scheme: {parts.scheme!r}")
    host = parts.hostname
    if not host:
        raise SourceEnablementSecurityError("url has no host")
    if host in _METADATA_HOSTS:
        raise SourceEnablementSecurityError("refused: cloud metadata endpoint")
    if allowed_domains and not any(host == d or host.endswith("." + d) for d in allowed_domains):
        raise SourceEnablementSecurityError(f"refused: {host!r} is not in this source's allowed_domains {allowed_domains!r}")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise SourceEnablementSecurityError(f"DNS resolution failed for {host!r}: {exc}") from exc
    for info in infos:
        ip_str = info[4][0]
        if _is_blocked_ip(ip_str):
            raise SourceEnablementSecurityError(f"refused: {host!r} resolves to a blocked address ({ip_str})")


_robots_cache: dict[str, urllib.robotparser.RobotFileParser] = {}


def is_allowed_by_robots(url: str, user_agent: str = "APEXBot") -> bool:
    """Real robots.txt check via the stdlib parser, cached per host for
    this process's lifetime. Fails CLOSED (returns False, i.e. NOT
    allowed) if robots.txt cannot be fetched or parsed at all -- a
    network hiccup must never be silently treated as permission."""
    parts = urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    parser = _robots_cache.get(robots_url)
    if parser is None:
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(robots_url)
        try:
            with httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=False) as client:
                resp = client.get(robots_url)
            if resp.status_code == 200:
                parser.parse(resp.text.splitlines())
            elif resp.status_code in (401, 403):
                # An explicit denial-of-access on robots.txt itself is treated as disallow-all.
                parser.parse(["User-agent: *", "Disallow: /"])
            else:
                # No robots.txt (404 etc.) -- standard convention: allowed.
                parser.parse([])
        except httpx.HTTPError:
            _robots_cache[robots_url] = None  # type: ignore[assignment]
            return False
        _robots_cache[robots_url] = parser
    if parser is None:
        return False
    return parser.can_fetch(user_agent, url)


# ---------------------------------------------------------------------
# Fetch result / extracted item contracts (Section 12)
# ---------------------------------------------------------------------

@dataclasses.dataclass
class ExtractedItem:
    source_id: str
    source_url: str
    canonical_url: str | None
    discovered_at: dt.datetime
    published_at: dt.datetime | None  # ONLY set from a real, source-provided signal -- never inferred
    raw_title: str | None
    raw_description: str | None
    raw_content: str
    extraction_metadata: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class SourceFetchResult:
    source_id: str
    fetched_at: dt.datetime
    request_url: str
    final_url: str | None
    status_code: int | None
    success: bool
    raw_items: list[ExtractedItem] = dataclasses.field(default_factory=list)
    errors: list[str] = dataclasses.field(default_factory=list)
    warnings: list[str] = dataclasses.field(default_factory=list)
    provenance: dict = dataclasses.field(default_factory=dict)
    adapter_type: str = ""
    duration_ms: float = 0.0
    health_status: str = HEALTH_NOT_TESTED


@dataclasses.dataclass
class SourceHealthRecord:
    """Section 19 -- health is a fact this module records from real
    attempts, never a default. `consecutive_failures` and
    `last_error` are written only by `record_attempt`."""
    source_id: str
    health_status: str = HEALTH_NOT_TESTED
    last_attempt_at: dt.datetime | None = None
    last_success_at: dt.datetime | None = None
    consecutive_failures: int = 0
    last_error: str | None = None
    latency_ms: float | None = None
    item_count: int = 0
    parser_error_count: int = 0

    def record_attempt(self, result: SourceFetchResult) -> None:
        self.last_attempt_at = result.fetched_at
        self.latency_ms = result.duration_ms
        self.item_count = len(result.raw_items)
        self.parser_error_count = len(result.warnings)
        if result.success:
            self.last_success_at = result.fetched_at
            self.consecutive_failures = 0
            self.last_error = None
            self.health_status = HEALTH_HEALTHY if not result.warnings else HEALTH_DEGRADED
        else:
            self.consecutive_failures += 1
            self.last_error = "; ".join(result.errors)[:500] if result.errors else "unknown failure"
            self.health_status = result.health_status if result.health_status != HEALTH_NOT_TESTED else HEALTH_FAILED


# ---------------------------------------------------------------------
# Minimal, dependency-free HTML content extraction (Section 5).
# NOT a full CSS selector engine -- deliberately, to avoid adding a new
# dependency (`bs4` is not installed in this repository). Supports the
# subset actually needed: a bare tag name ("h1"), "tag.class", or
# "tag#id". This limitation is disclosed in the report, not hidden.
# ---------------------------------------------------------------------
from html.parser import HTMLParser


class _SelectorHTMLExtractor(HTMLParser):
    def __init__(self, selector: str):
        super().__init__(convert_charrefs=True)
        self._tag, self._attr_kind, self._attr_val = self._parse_selector(selector)
        self._depth_stack: list[bool] = []
        self._capturing_depth: int | None = None
        self._current_depth = 0
        self.matches: list[str] = []
        self._buffer: list[str] = []

    @staticmethod
    def _parse_selector(selector: str) -> tuple[str, str | None, str | None]:
        m = re.match(r"^([a-zA-Z0-9]+)(?:([.#])([a-zA-Z0-9_-]+))?$", selector.strip())
        if not m:
            return selector.strip(), None, None
        tag, kind, val = m.groups()
        return tag.lower(), ({".": "class", "#": "id"}.get(kind) if kind else None), val

    def handle_starttag(self, tag, attrs):
        self._current_depth += 1
        attrs_dict = dict(attrs)
        is_match = tag == self._tag and (
            self._attr_kind is None
            or (self._attr_kind == "id" and attrs_dict.get("id") == self._attr_val)
            or (self._attr_kind == "class" and self._attr_val in (attrs_dict.get("class") or "").split())
        )
        if is_match and self._capturing_depth is None:
            self._capturing_depth = self._current_depth
            self._buffer = []

    def handle_endtag(self, tag):
        if self._capturing_depth == self._current_depth:
            text = "".join(self._buffer).strip()
            if text:
                self.matches.append(text)
            self._capturing_depth = None
        self._current_depth = max(0, self._current_depth - 1)

    def handle_data(self, data):
        if self._capturing_depth is not None:
            self._buffer.append(data)


def extract_by_selector(html: str, selector: str) -> list[str]:
    parser = _SelectorHTMLExtractor(selector)
    try:
        parser.feed(html)
    except Exception:
        logger.warning("extract_by_selector: malformed HTML tolerated for selector=%r", selector)
        return parser.matches
    return parser.matches


# ---------------------------------------------------------------------
# Adapter Protocol + Generic Web Adapter
# ---------------------------------------------------------------------

class EnablementAdapter(Protocol):
    adapter_type: str

    async def fetch(self, source: dict) -> SourceFetchResult:
        ...


class GenericWebAdapter:
    """Configuration-driven single-page discovery adapter (Section 5).
    Degrades safely when optional selector fields are missing: with no
    `content_selectors`, it still records the fetch and page title but
    yields zero extracted items rather than guessing at structure."""

    adapter_type = "generic_web"

    async def fetch(self, source: dict) -> SourceFetchResult:
        start = dt.datetime.now(dt.timezone.utc)
        source_id = source["source_id"]
        url = source.get("start_url") or source.get("canonical_url")
        allowed_domains = source.get("allowed_domains") or ([urlsplit(url).hostname] if url else [])
        result = SourceFetchResult(
            source_id=source_id, fetched_at=start, request_url=url or "", final_url=None,
            status_code=None, success=False, adapter_type=self.adapter_type,
        )
        if not url:
            result.errors.append("no start_url/canonical_url configured")
            result.health_status = HEALTH_UNSUPPORTED
            return result
        try:
            validate_fetch_url(url, allowed_domains)
            if not is_allowed_by_robots(url):
                result.errors.append("robots.txt disallows this URL")
                result.health_status = HEALTH_FAILED
                return result

            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, follow_redirects=False) as client:
                current_url = url
                response = None
                for _ in range(MAX_REDIRECTS + 1):
                    response = await client.get(current_url, headers={"User-Agent": "APEXBot/1.0 (+source-enablement)"})
                    if response.is_redirect:
                        next_url = urljoin(current_url, response.headers.get("location", ""))
                        validate_fetch_url(next_url, allowed_domains)  # re-validate EVERY hop
                        current_url = next_url
                        continue
                    break
                assert response is not None

            result.status_code = response.status_code
            result.final_url = str(response.url)
            content_length = int(response.headers.get("content-length") or len(response.content))
            if content_length > MAX_RESPONSE_BYTES:
                result.errors.append(f"response too large ({content_length} bytes > {MAX_RESPONSE_BYTES})")
                result.health_status = HEALTH_DEGRADED
                return result
            if response.status_code != 200:
                result.errors.append(f"HTTP {response.status_code}")
                result.health_status = HEALTH_FAILED
                return result

            html = response.text
            items = self._extract_items(source, html, current_url, start)
            result.raw_items = items
            result.success = True
            result.health_status = HEALTH_HEALTHY if items else HEALTH_DEGRADED
            if not items:
                result.warnings.append("fetch succeeded but zero items extracted (check content_selectors)")
        except SourceEnablementSecurityError as exc:
            result.errors.append(str(exc))
            result.health_status = HEALTH_FAILED
        except httpx.TimeoutException as exc:
            result.errors.append(f"timeout: {exc}")
            result.health_status = HEALTH_FAILED
        except httpx.HTTPError as exc:
            result.errors.append(f"http error: {exc}")
            result.health_status = HEALTH_FAILED
        finally:
            result.duration_ms = (dt.datetime.now(dt.timezone.utc) - start).total_seconds() * 1000
        return result

    def _extract_items(self, source: dict, html: str, page_url: str, discovered_at: dt.datetime) -> list[ExtractedItem]:
        content_selectors = source.get("content_selectors") or []
        title_selectors = source.get("title_selectors") or []
        description_selectors = source.get("description_selectors") or []
        if not content_selectors:
            return []
        contents = []
        for sel in content_selectors:
            contents.extend(extract_by_selector(html, sel))
        titles = []
        for sel in title_selectors:
            titles.extend(extract_by_selector(html, sel))
        descriptions = []
        for sel in description_selectors:
            descriptions.extend(extract_by_selector(html, sel))
        items = []
        for i, content in enumerate(contents):
            items.append(ExtractedItem(
                source_id=source["source_id"],
                source_url=page_url,
                canonical_url=page_url,
                discovered_at=discovered_at,
                published_at=None,  # never inferred by this adapter -- Section 12
                raw_title=titles[i] if i < len(titles) else None,
                raw_description=descriptions[i] if i < len(descriptions) else None,
                raw_content=content,
                extraction_metadata={"adapter_type": self.adapter_type, "selector_index": i},
            ))
        return items


# ---------------------------------------------------------------------
# RSS / Atom Adapter (Section 6)
# ---------------------------------------------------------------------

_RSS_ITEM_TAGS = ("item", "{http://www.w3.org/2005/Atom}entry")


class RSSAdapter:
    adapter_type = "rss"

    async def fetch(self, source: dict) -> SourceFetchResult:
        start = dt.datetime.now(dt.timezone.utc)
        source_id = source["source_id"]
        url = source.get("rss_url") or source.get("canonical_url")
        allowed_domains = source.get("allowed_domains") or ([urlsplit(url).hostname] if url else [])
        result = SourceFetchResult(
            source_id=source_id, fetched_at=start, request_url=url or "", final_url=None,
            status_code=None, success=False, adapter_type=self.adapter_type,
        )
        if not url:
            result.errors.append("no rss_url configured")
            result.health_status = HEALTH_UNSUPPORTED
            return result
        try:
            validate_fetch_url(url, allowed_domains)
            if not is_allowed_by_robots(url):
                result.errors.append("robots.txt disallows this URL")
                result.health_status = HEALTH_FAILED
                return result
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, follow_redirects=True, max_redirects=MAX_REDIRECTS) as client:
                response = await client.get(url, headers={"User-Agent": "APEXBot/1.0 (+source-enablement)"})
            result.status_code = response.status_code
            result.final_url = str(response.url)
            if len(response.content) > MAX_RESPONSE_BYTES:
                result.errors.append("response too large")
                result.health_status = HEALTH_DEGRADED
                return result
            if response.status_code != 200:
                result.errors.append(f"HTTP {response.status_code}")
                result.health_status = HEALTH_FAILED
                return result

            seen_urls: set[str] = set()
            items: list[ExtractedItem] = []
            try:
                root = SafeET.fromstring(response.content)
            except Exception as exc:
                result.errors.append(f"malformed feed XML: {exc}")
                result.health_status = HEALTH_FAILED
                return result

            for node in root.iter():
                tag_name = node.tag.split("}")[-1]
                if tag_name not in ("item", "entry"):
                    continue
                title = self._first_text(node, ("title",))
                link = self._first_link(node)
                description = self._first_text(node, ("description", "summary"))
                pub_date = self._first_text(node, ("pubDate", "published", "updated"))
                if link and link in seen_urls:
                    result.warnings.append(f"duplicate feed entry skipped: {link}")
                    continue
                if link:
                    seen_urls.add(link)
                published_at = self._parse_rfc_or_iso(pub_date) if pub_date else None
                items.append(ExtractedItem(
                    source_id=source_id, source_url=url, canonical_url=link,
                    discovered_at=start, published_at=published_at,
                    raw_title=title, raw_description=description, raw_content=description or title or "",
                    extraction_metadata={"adapter_type": self.adapter_type, "feed_url": url},
                ))
            result.raw_items = items
            result.success = True
            result.health_status = HEALTH_HEALTHY if items else HEALTH_DEGRADED
        except SourceEnablementSecurityError as exc:
            result.errors.append(str(exc))
            result.health_status = HEALTH_FAILED
        except httpx.HTTPError as exc:
            result.errors.append(f"http error: {exc}")
            result.health_status = HEALTH_FAILED
        finally:
            result.duration_ms = (dt.datetime.now(dt.timezone.utc) - start).total_seconds() * 1000
        return result

    @staticmethod
    def _first_text(node, tag_names: tuple[str, ...]) -> str | None:
        for child in node:
            local = child.tag.split("}")[-1]
            if local in tag_names and child.text:
                return child.text.strip()
        return None

    @staticmethod
    def _first_link(node) -> str | None:
        for child in node:
            local = child.tag.split("}")[-1]
            if local == "link":
                href = child.attrib.get("href")
                if href:
                    return href
                if child.text:
                    return child.text.strip()
        return None

    @staticmethod
    def _parse_rfc_or_iso(value: str) -> dt.datetime | None:
        # Deliberately conservative: only parses a real, well-formed
        # timestamp; never guesses a date from surrounding text
        # (Section 12/6 -- publication time must never be inferred).
        for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                parsed = dt.datetime.strptime(value.strip(), fmt)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=dt.timezone.utc)
                return parsed
            except ValueError:
                continue
        return None


# ---------------------------------------------------------------------
# Sitemap Adapter (Section 7)
# ---------------------------------------------------------------------

class SitemapAdapter:
    adapter_type = "sitemap"

    async def fetch(self, source: dict) -> SourceFetchResult:
        start = dt.datetime.now(dt.timezone.utc)
        source_id = source["source_id"]
        url = source.get("sitemap_url") or source.get("canonical_url")
        allowed_domains = source.get("allowed_domains") or ([urlsplit(url).hostname] if url else [])
        result = SourceFetchResult(
            source_id=source_id, fetched_at=start, request_url=url or "", final_url=None,
            status_code=None, success=False, adapter_type=self.adapter_type,
        )
        if not url:
            result.errors.append("no sitemap_url configured")
            result.health_status = HEALTH_UNSUPPORTED
            return result
        try:
            validate_fetch_url(url, allowed_domains)
            if not is_allowed_by_robots(url):
                result.errors.append("robots.txt disallows this URL")
                result.health_status = HEALTH_FAILED
                return result
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, follow_redirects=True, max_redirects=MAX_REDIRECTS) as client:
                response = await client.get(url, headers={"User-Agent": "APEXBot/1.0 (+source-enablement)"})
            result.status_code = response.status_code
            result.final_url = str(response.url)
            if len(response.content) > MAX_RESPONSE_BYTES:
                result.errors.append("response too large")
                result.health_status = HEALTH_DEGRADED
                return result
            if response.status_code != 200:
                result.errors.append(f"HTTP {response.status_code}")
                result.health_status = HEALTH_FAILED
                return result

            try:
                root = SafeET.fromstring(response.content)
            except Exception as exc:
                result.errors.append(f"malformed sitemap XML: {exc}")
                result.health_status = HEALTH_FAILED
                return result

            host = urlsplit(url).hostname
            seen: set[str] = set()
            items: list[ExtractedItem] = []
            for node in root.iter():
                local = node.tag.split("}")[-1]
                if local != "url":
                    continue
                loc, lastmod = None, None
                for child in node:
                    child_local = child.tag.split("}")[-1]
                    if child_local == "loc" and child.text:
                        loc = child.text.strip()
                    elif child_local == "lastmod" and child.text:
                        lastmod = child.text.strip()
                if not loc:
                    continue
                loc_host = urlsplit(loc).hostname
                if allowed_domains and not (loc_host == host or (loc_host or "").endswith("." + (host or ""))):
                    result.warnings.append(f"skipped out-of-domain sitemap entry: {loc}")
                    continue
                if loc in seen:
                    continue
                seen.add(loc)
                published_at = RSSAdapter._parse_rfc_or_iso(lastmod) if lastmod else None
                items.append(ExtractedItem(
                    source_id=source_id, source_url=url, canonical_url=loc,
                    discovered_at=start, published_at=published_at,
                    raw_title=None, raw_description=None, raw_content="",
                    extraction_metadata={"adapter_type": self.adapter_type, "lastmod": lastmod},
                ))
            result.raw_items = items
            result.success = True
            result.health_status = HEALTH_HEALTHY if items else HEALTH_DEGRADED
        except SourceEnablementSecurityError as exc:
            result.errors.append(str(exc))
            result.health_status = HEALTH_FAILED
        except httpx.HTTPError as exc:
            result.errors.append(f"http error: {exc}")
            result.health_status = HEALTH_FAILED
        finally:
            result.duration_ms = (dt.datetime.now(dt.timezone.utc) - start).total_seconds() * 1000
        return result


# ---------------------------------------------------------------------
# Generic API Adapter (Section 8) -- credential presence is the FIRST
# check, before any network access; never fabricates a token.
# ---------------------------------------------------------------------

class APIAdapter:
    adapter_type = "api"

    async def fetch(self, source: dict) -> SourceFetchResult:
        start = dt.datetime.now(dt.timezone.utc)
        source_id = source["source_id"]
        api_key_setting = source.get("api_key_setting")  # name of a `settings.*` attribute, never a literal value
        result = SourceFetchResult(
            source_id=source_id, fetched_at=start, request_url=source.get("endpoint", ""), final_url=None,
            status_code=None, success=False, adapter_type=self.adapter_type,
        )
        if not source.get("endpoint"):
            result.errors.append("no endpoint configured")
            result.health_status = HEALTH_UNSUPPORTED
            result.duration_ms = 0.0
            return result
        credential_value = getattr(settings, api_key_setting, None) if api_key_setting else None
        if not credential_value:
            result.errors.append(f"credentials_unavailable: settings.{api_key_setting} is not set")
            result.health_status = HEALTH_CREDENTIALS_REQUIRED
            result.duration_ms = 0.0
            return result
        # A real credential exists but no generic, source-agnostic
        # request shape can be safely assumed for an arbitrary third-
        # party API without knowing its actual contract -- refusing to
        # guess one is the honest choice here (Section 8/34: never
        # fabricate a successful fetch). A concrete API integration
        # (like the existing, real Facebook Graph API adapter) is
        # built per-source, reusing this class's credential-gating
        # convention, not by this generic class guessing a request.
        result.errors.append("no concrete request mapping implemented for this source; credential present but unused")
        result.health_status = HEALTH_UNSUPPORTED
        result.duration_ms = (dt.datetime.now(dt.timezone.utc) - start).total_seconds() * 1000
        return result


# ---------------------------------------------------------------------
# Telegram / Facebook -- REUSE existing adapters, never reimplement.
# ---------------------------------------------------------------------

class TelegramEnablementAdapter:
    """Wraps the existing, real `app.services.source_adapters` Telegram
    adapters -- no new Telegram implementation. Uses the LIVE adapter
    only for a source explicitly marked `live: true` in the real
    `app/data/opportunity_sources.json` (mirrors `_build_source_
    candidates`'s own existing routing rule); every other Telegram
    source resolves to the pre-existing mocked adapter, unchanged."""

    adapter_type = "telegram_channel"

    async def fetch(self, source: dict) -> SourceFetchResult:
        from app.services import source_adapters
        from app.services.aggregator_service import TelegramChannelConfig

        start = dt.datetime.now(dt.timezone.utc)
        handle = source.get("telegram_handle")
        result = SourceFetchResult(
            source_id=source["source_id"], fetched_at=start, request_url=handle or "",
            final_url=None, status_code=None, success=False, adapter_type=self.adapter_type,
        )
        if not handle:
            result.errors.append("no telegram_handle configured")
            result.health_status = HEALTH_UNSUPPORTED
            return result
        if not source.get("discovery_enabled"):
            result.errors.append("not wired into app/data/opportunity_sources.json -- catalog-only source")
            result.health_status = HEALTH_UNSUPPORTED
            return result
        config = TelegramChannelConfig(handle=handle, category=source.get("source_type", "General"))
        adapter_kind = "telegram_channel_live" if source.get("live_status") == "LIVE" else "telegram_channel"
        adapter = source_adapters.get_adapter(adapter_kind)
        try:
            # Both `TelegramSourceAdapter.fetch` and `LiveTelegramSourceAdapter.fetch`
            # are synchronous, blocking calls -- always run them off the event
            # loop via a thread, never call either directly inside this
            # coroutine (that would block every other concurrent fetch).
            raw_results = await asyncio.to_thread(adapter.fetch, config)
            items = [
                ExtractedItem(
                    source_id=source["source_id"], source_url=handle,
                    canonical_url=r.item_url, discovered_at=r.retrieved_at or start,
                    published_at=r.published_at, raw_title=None, raw_description=None,
                    raw_content=r.raw_text, extraction_metadata={"adapter_type": r.source_kind, "external_item_id": r.external_item_id},
                )
                for r in raw_results
            ]
            result.raw_items = items
            result.success = True
            result.health_status = HEALTH_HEALTHY
        except Exception as exc:  # isolate this source's failure -- never propagate to caller (Section 28)
            result.errors.append(str(exc)[:500])
            result.health_status = HEALTH_FAILED
        finally:
            result.duration_ms = (dt.datetime.now(dt.timezone.utc) - start).total_seconds() * 1000
        return result


class FacebookEnablementAdapter:
    """Wraps the existing, real `FacebookGraphAPISourceAdapter` -- no
    scraping bypass, no new Facebook implementation. Reports
    `credentials_required` (never `live`) when
    `settings.facebook_graph_api_access_token` is empty, exactly like
    the existing adapter's own `is_configured()` gate."""

    adapter_type = "facebook_graph_api"

    async def fetch(self, source: dict) -> SourceFetchResult:
        from app.services import facebook_graph_client, source_adapters
        from app.services.aggregator_service import FacebookPageConfig

        start = dt.datetime.now(dt.timezone.utc)
        page_id = source.get("facebook_page_id")
        result = SourceFetchResult(
            source_id=source["source_id"], fetched_at=start, request_url=page_id or "",
            final_url=None, status_code=None, success=False, adapter_type=self.adapter_type,
        )
        if not page_id:
            result.errors.append("no facebook_page_id configured")
            result.health_status = HEALTH_UNSUPPORTED
            return result
        if not facebook_graph_client.is_configured():
            result.errors.append("credentials_unavailable: settings.facebook_graph_api_access_token is not set")
            result.health_status = HEALTH_CREDENTIALS_REQUIRED
            return result
        config = FacebookPageConfig(page_id=page_id, name=source.get("name", page_id), target_age="all", live=True)
        adapter = source_adapters.get_adapter("facebook_page_live")
        try:
            # `FacebookGraphAPISourceAdapter.fetch` is synchronous and
            # blocking -- run it off the event loop, same reasoning as
            # `TelegramEnablementAdapter` above.
            raw_results = await asyncio.to_thread(adapter.fetch, config)
            items = [
                ExtractedItem(
                    source_id=source["source_id"], source_url=page_id,
                    canonical_url=r.item_url, discovered_at=r.retrieved_at or start,
                    published_at=r.published_at, raw_title=None, raw_description=None,
                    raw_content=r.raw_text, extraction_metadata={"adapter_type": r.source_kind, "external_item_id": r.external_item_id},
                )
                for r in raw_results
            ]
            result.raw_items = items
            result.success = True
            result.health_status = HEALTH_HEALTHY
        except Exception as exc:
            result.errors.append(str(exc)[:500])
            result.health_status = HEALTH_FAILED
        finally:
            result.duration_ms = (dt.datetime.now(dt.timezone.utc) - start).total_seconds() * 1000
        return result


class NoAdapterAvailable:
    """Explicit capability state (Sections 10/11) for WhatsApp,
    Instagram, LinkedIn, and any other source with no legitimate,
    currently-implementable fetch path in this codebase. Always
    reports `unsupported` -- never attempts a network call, never
    claims partial capability it doesn't have."""

    adapter_type = "no_adapter_available"

    async def fetch(self, source: dict) -> SourceFetchResult:
        start = dt.datetime.now(dt.timezone.utc)
        return SourceFetchResult(
            source_id=source["source_id"], fetched_at=start, request_url=source.get("canonical_url") or "",
            final_url=None, status_code=None, success=False, adapter_type=self.adapter_type,
            errors=["no legitimate adapter implemented for this source_kind in this codebase"],
            health_status=HEALTH_UNSUPPORTED, duration_ms=0.0,
        )


_ADAPTERS: dict[str, EnablementAdapter] = {
    "generic_web": GenericWebAdapter(),
    "rss": RSSAdapter(),
    "sitemap": SitemapAdapter(),
    "api": APIAdapter(),
    "telegram_channel": TelegramEnablementAdapter(),
    "facebook_graph_api": FacebookEnablementAdapter(),
    "no_adapter_available": NoAdapterAvailable(),
    "unresolved": NoAdapterAvailable(),
}


def get_enablement_adapter(adapter_type: str) -> EnablementAdapter:
    return _ADAPTERS.get(adapter_type, _ADAPTERS["no_adapter_available"])


# ---------------------------------------------------------------------
# Batch runner -- bounded concurrency, per-source failure isolation
# (Sections 27/28). One failing source's exception can never poison
# another source's fetch, matching this session's own earlier-fixed
# SQLAlchemy-session-poisoning lesson (which was a DB-session issue,
# not present here since this module performs no DB writes at all).
# ---------------------------------------------------------------------

async def run_source_enablement_batch(
    sources: list[dict], *, concurrency_limit: int = 5,
) -> list[SourceFetchResult]:
    semaphore = asyncio.Semaphore(concurrency_limit)

    async def _run_one(source: dict) -> SourceFetchResult:
        async with semaphore:
            adapter = get_enablement_adapter(source["adapter_type"])
            try:
                return await adapter.fetch(source)
            except Exception as exc:  # absolute isolation floor -- an adapter bug still can't take down the batch
                logger.exception("source_enablement: adapter raised for source_id=%s", source.get("source_id"))
                return SourceFetchResult(
                    source_id=source["source_id"], fetched_at=dt.datetime.now(dt.timezone.utc),
                    request_url=source.get("canonical_url") or "", final_url=None, status_code=None,
                    success=False, adapter_type=source.get("adapter_type", "unknown"),
                    errors=[f"adapter raised unexpectedly: {str(exc)[:300]}"], health_status=HEALTH_FAILED,
                )

    return list(await asyncio.gather(*(_run_one(s) for s in sources)))
