"""Phase 3B: Real Source Adapters & Live Discovery Execution.

Facebook Graph API adapter -- INTERFACE + CONFIGURATION CONTRACT ONLY,
per this phase's own explicit instruction (Section 5): "If credentials/
permissions are unavailable: implement the adapter interface and
configuration contract only if that is architecturally useful, but DO
NOT claim live integration."

WHY FACEBOOK IS NOT TREATED LIKE TELEGRAM: Telegram's `t.me/s/<handle>`
preview page is a genuinely public page with no `robots.txt` disallowing
it (confirmed this phase). Facebook is the opposite -- confirmed during
this project's own Phase 3A source-vetting work that `facebook.com`'s
`robots.txt` disallows automated fetching of page content
(`WebFetch` returned `ROBOTS_DISALLOWED` for `facebook.com/officialchevening`
during that phase), and real per-post content requires the official
Graph API with a real app access token and page permissions
(`pages_read_engagement` or similar) -- neither of which exists in this
environment (`settings.facebook_graph_api_access_token` is empty by
default, same honest-availability convention as
`research_orchestrator._provider_available`'s AI provider keys).

This module therefore:
1. Implements the REAL Graph API request shape (`GET
   /v19.0/{page_id}/posts?fields=...`) so a real token can be dropped
   into `FACEBOOK_GRAPH_API_ACCESS_TOKEN` and used with zero further
   code changes -- proven by `TestFacebookGraphClientConfiguredMocked`
   in this phase's own test suite, which supplies a fake-but-realistic
   token via `httpx.MockTransport` and confirms this module builds the
   correct real request and parses a real-shaped response correctly.
2. Refuses to run at all when unconfigured -- raising
   `SourceNotConfiguredError` (never returning fabricated/mocked posts,
   never silently falling through to the OLD, still-separately-mocked
   `fetch_new_facebook_posts` path).
3. Is NOT registered in `app.services.source_adapters.ADAPTERS` and no
   entry in `app/data/opportunity_sources.json` requests it -- every one
   of the 7 configured Facebook pages keeps using the pre-existing,
   unchanged, disclosed-mocked `FacebookSourceAdapter` exactly as
   before this phase. Wiring a real page over to this adapter is a
   future, separate, credentialed decision -- this phase only proves
   the code is real and ready, honestly labeled BLOCKED for live use
   today.

Never bypasses Facebook's own access controls, never scrapes page HTML,
never uses an unofficial/unauthorized access method (Phase 3B Sections
4/5) -- the ONLY access path this module implements is the official,
documented Graph API, gated on a real, user-supplied credential."""

from __future__ import annotations

import logging

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.services.aggregator_service import FacebookPageConfig
from app.services.temporal_evidence import parse_source_timestamp

logger = logging.getLogger(__name__)

_GRAPH_API_BASE = "https://graph.facebook.com/v19.0"
_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
_MAX_POSTS_PER_PAGE = 25  # bounded, single-page fetch -- no pagination in this phase's scope.


class FacebookGraphAPIError(Exception):
    """Base class for classified Graph API failures."""


class SourceNotConfiguredError(FacebookGraphAPIError):
    """Raised by `fetch` when no real access token is configured. This
    is a HONEST, real, expected failure in every environment without a
    provisioned Graph API credential -- never silently swallowed into
    an empty-but-successful result, so `_poll_source`'s error-isolation
    path records it as a real, visible per-source failure rather than a
    quiet zero-item success."""


class FacebookGraphAPITransientError(FacebookGraphAPIError):
    """Timeout, connection error, or HTTP 5xx/429 -- may succeed on a
    bounded retry."""


class FacebookGraphAPIPermanentError(FacebookGraphAPIError):
    """HTTP 401/403 (bad/expired/under-permissioned token) or 404
    (unknown page id) -- never retried; retrying an invalid credential
    repeatedly is explicitly disallowed (Phase 3B Section 12)."""


def is_configured() -> bool:
    """Same honest-availability posture as
    `research_orchestrator._provider_available`: a real, non-empty
    token, never assumed present."""
    token = settings.facebook_graph_api_access_token
    return bool(token) and str(token).strip() != ""


_retry_transient = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception_type((FacebookGraphAPITransientError, httpx.TimeoutException, httpx.ConnectError)),
)


@_retry_transient
def _fetch_posts_page(client: httpx.Client, page_id: str, token: str) -> list[dict]:
    """One bounded, real Graph API request. Never logs `token` (Section
    25 -- no secret in logs/exceptions): any raised message references
    only the page id and status code."""
    url = f"{_GRAPH_API_BASE}/{page_id}/posts"
    params = {
        "fields": "id,message,created_time,permalink_url",
        "limit": _MAX_POSTS_PER_PAGE,
        "access_token": token,
    }
    try:
        response = client.get(url, params=params)
    except httpx.TimeoutException as exc:
        raise FacebookGraphAPITransientError(f"timeout fetching Graph API posts for page {page_id!r}") from exc
    except httpx.ConnectError as exc:
        raise FacebookGraphAPITransientError(f"connection error fetching Graph API posts for page {page_id!r}") from exc
    except httpx.HTTPError as exc:
        raise FacebookGraphAPITransientError(f"transport error fetching Graph API posts for page {page_id!r}: {exc}") from exc

    if response.status_code in (401, 403, 404):
        raise FacebookGraphAPIPermanentError(f"permanent HTTP {response.status_code} for page {page_id!r}")
    if response.status_code == 429 or 500 <= response.status_code < 600:
        raise FacebookGraphAPITransientError(f"transient HTTP {response.status_code} for page {page_id!r}")
    if response.status_code != 200:
        raise FacebookGraphAPIPermanentError(f"unexpected HTTP {response.status_code} for page {page_id!r}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise FacebookGraphAPIPermanentError(f"non-JSON response for page {page_id!r}") from exc

    data = payload.get("data")
    if not isinstance(data, list):
        raise FacebookGraphAPIPermanentError(f"unexpected Graph API response shape for page {page_id!r}")
    return data


def _normalize_graph_timestamp(raw: str | None) -> str | None:
    """Graph API's own real `created_time` shape is
    `"2026-09-10T12:34:56+0000"` (no colon in the UTC offset) --
    `datetime.fromisoformat` on this repo's Python 3.10 runtime requires
    a colon (`+00:00`). This inserts exactly that colon when the shape
    matches a trailing 4-digit numeric offset, and leaves everything
    else (already-valid ISO 8601, or genuinely malformed input)
    untouched for `parse_source_timestamp`'s own strict parser to
    accept or safely discard -- never a fuzzy/regex date HUNT (Phase 1's
    own anti-injection posture), only a fixed-position punctuation fix
    on a value already known to be a dedicated timestamp field."""
    if not raw or not isinstance(raw, str):
        return raw
    if len(raw) >= 5 and raw[-5] in "+-" and raw[-4:].isdigit():
        return f"{raw[:-2]}:{raw[-2:]}"
    return raw


def fetch_facebook_page_posts_live(config: FacebookPageConfig) -> list[dict]:
    """Real, bounded, single-page Graph API fetch. Raises
    `SourceNotConfiguredError` immediately (no network call at all) when
    no real token is configured -- this is the expected, honest,
    documented outcome in every environment today."""
    if not is_configured():
        raise SourceNotConfiguredError(
            f"Facebook Graph API not configured (settings.facebook_graph_api_access_token is empty) -- "
            f"cannot live-fetch page {config.page_id!r}"
        )

    token = settings.facebook_graph_api_access_token
    with httpx.Client(timeout=_TIMEOUT) as client:
        raw_posts = _fetch_posts_page(client, config.page_id, token)

    results: list[dict] = []
    for post in raw_posts:
        message = post.get("message")
        if not message or not isinstance(message, str):
            continue  # a real post with no text content (photo-only, etc.) -- nothing to extract from.
        results.append(
            {
                "external_item_id": post.get("id"),
                "item_url": post.get("permalink_url") or f"https://www.facebook.com/{config.page_id}",
                "raw_text": message[:8000],
                "published_at": parse_source_timestamp(_normalize_graph_timestamp(post.get("created_time"))),
            }
        )
    return results
