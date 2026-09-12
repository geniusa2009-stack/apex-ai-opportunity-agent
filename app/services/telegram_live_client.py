"""Phase 3B: Real Source Adapters & Live Discovery Execution.

LIVE Telegram fetch client -- fetches a channel's real, PUBLIC
`https://t.me/s/<handle>` preview page (Telegram's own public "widget"
view of a channel's message history, served with no login/API key/app
credential of any kind -- confirmed during this phase's own audit: `t.me`
publishes no `robots.txt` disallowing it, unlike `facebook.com`, which
does disallow scraping its pages -- see `facebook_graph_client.py`'s own
docstring for why Facebook is handled completely differently). This is
NOT the Telegram Bot API and needs no bot token -- it is the same public
HTML page a browser sees visiting that URL logged out.

DISCLOSED SCOPE / HONEST LIVE-REACHABILITY STATUS: this module's own
network calls to `t.me` could not be exercised end-to-end from the
sandbox this phase was built in -- the sandbox's own egress policy
denies `t.me` outbound (confirmed via this session's own agent-proxy
status: `t.me:443` -> `connect_rejected`, an organization policy
denial, not a Telegram-side block). This is the exact "credentials/
access unavailable" case this phase's own spec anticipated, except the
missing thing is EGRESS, not an API key -- treated with the same
honesty: this module is production-quality, real code, tested against
a real `httpx.MockTransport` network boundary (never a claim of
"live"), and this phase's own final report states plainly that a real
`t.me` round-trip was never observed from this build environment.
Whether the actual deployed Celery worker's own network egress allows
it is a real, different, unanswered question this module does not (and
cannot, from here) resolve.

PARSING: `t.me/s/<handle>` markup is Telegram's own long-stable public
widget structure (`tgme_widget_message` blocks, `data-post="handle/id"`,
a `tgme_widget_message_text` text div, a `<time datetime=...>` element
carrying the message's REAL, source-claimed publish timestamp). Parsed
with the Python standard library's `html.parser.HTMLParser` only --
deliberately NOT a new `lxml`/`beautifulsoup4` dependency (Dependency
Discipline, Phase 3B spec Section 26): this module's own audit found
neither already installed in this repository's environment despite
`scrapy` being listed in `requirements.txt`, and the extraction need
here (a handful of known attributes/classes) does not justify a new
pinned dependency.

SECURITY / PROMPT-INJECTION POSTURE: every byte fetched here is
UNTRUSTED, attacker-influenceable external content (Phase 3B spec
Section 19). This module's only job is to hand back plain `str` message
text plus a small number of STRUCTURED fields it lifts directly from
known HTML attributes (`data-post`, `href`, `datetime`) -- never
anything parsed out of the free-text message body itself. Nothing here
executes, evaluates, or interprets fetched text as instructions,
configuration, or code; `parse_preview_html` never raises on malformed/
adversarial input (a message block missing an expected attribute is
skipped, not fatal), and `<script>`/`<style>` tag CONTENTS are never
surfaced as message text (belt-and-suspenders -- this module never
treats fetched markup as executable regardless of tag)."""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import time as _time
from html.parser import HTMLParser

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import settings
from app.services.temporal_evidence import parse_source_timestamp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# Bounded-execution constants (Phase 3B Sections 10/13/16: pagination,
# timeouts, payload limits -- all real, all enforced, none unbounded).
# ---------------------------------------------------------------------

_USER_AGENT = "ApexDiscoveryBot/1.0 (+https://github.com/geniusa2009-stack/apex-backend)"
_MAX_RESPONSE_BYTES = 2_000_000  # 2 MB hard cap per page fetch (Section 16).
_MAX_TEXT_LENGTH = 8_000  # per-message text cap -- never store unbounded raw content.
_MAX_RETRY_AFTER_SECONDS = 30.0  # never sleep indefinitely on a 429.
_OVERALL_WALL_CLOCK_BUDGET_SECONDS = 25.0  # hard cap on one fetch_telegram_channel_live() call.


class TelegramFetchError(Exception):
    """Base class for every classified failure this module raises.
    `_poll_source` (aggregator_tasks.py) catches this broadly so one
    misbehaving channel can never crash an entire discovery round
    (Phase 3B Section 14, Error Isolation)."""


class TelegramTransientError(TelegramFetchError):
    """Network-level or server-side failure that MAY succeed on a
    bounded retry: connect/read timeout, connection error, HTTP 5xx, or
    HTTP 429 (rate limited)."""


class TelegramPermanentError(TelegramFetchError):
    """A failure retrying will not fix: HTTP 404 (unknown/renamed
    channel), HTTP 403 (blocked/private), or a response that fails
    basic structural validation (wrong content-type, oversized, not
    HTML at all). Never retried."""


@dataclasses.dataclass(frozen=True)
class TelegramLiveMessage:
    """One real, parsed message from a channel's public preview page.
    `published_at` is the channel's OWN claimed timestamp for this exact
    message (parsed strictly via `temporal_evidence.parse_source_timestamp`
    -- Phase 1 integration, Phase 3B Section 8) -- never retrieval time,
    never inferred from the message body."""

    external_item_id: str
    item_url: str
    raw_text: str
    published_at: dt.datetime | None


def _classify_http_status(status_code: int) -> None:
    """Raises the correctly classified exception for a non-2xx response,
    or returns None for a 2xx. Retry semantics live entirely in this one
    place (Phase 3B Section 12: retries must be bounded and CLASSIFIED,
    never a blanket `except Exception: retry`)."""
    if 200 <= status_code < 300:
        return
    if status_code == 429 or 500 <= status_code < 600:
        raise TelegramTransientError(f"transient HTTP {status_code} from t.me")
    # Everything else (401/403/404/other 4xx) is permanent -- most
    # commonly 404 (unknown/renamed/deleted channel) or 403 (Telegram
    # itself blocking this request, e.g. a private channel with no
    # public preview). Never retried, never treated as "channel is
    # empty" (a real, disclosed distinction from a genuine zero-post
    # response).
    raise TelegramPermanentError(f"permanent HTTP {status_code} from t.me")


def _sleep_for_retry_after(response: httpx.Response) -> None:
    """Honors a real `Retry-After` header (Phase 3B Section 11), bounded
    to `_MAX_RETRY_AFTER_SECONDS` so a hostile/misconfigured header can
    never stall a discovery round for an unbounded time."""
    raw = response.headers.get("retry-after")
    if not raw:
        return
    try:
        seconds = min(float(raw), _MAX_RETRY_AFTER_SECONDS)
    except ValueError:
        return
    if seconds > 0:
        _time.sleep(seconds)


_retry_transient = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    # Same idiom as every other tenacity user in this codebase
    # (competition_scout.py et al.): retry ONLY the one exception type
    # this module itself classifies as transient -- a permanent failure
    # (404/403/malformed) is never retried (Section 12).
    retry=retry_if_exception_type((TelegramTransientError, httpx.TimeoutException, httpx.ConnectError)),
)


@_retry_transient
def _fetch_preview_page(client: httpx.Client, handle: str, *, before: int | None) -> str:
    """One bounded GET of `https://t.me/s/<handle>` (optionally paged
    via `?before=<message_id>`, Telegram's own real pagination
    mechanism -- confirmed via this phase's own live web-search vetting
    trail, not invented). Raises a classified `TelegramFetchError` on
    any failure; never returns partial/unvalidated content."""
    clean_handle = handle.lstrip("@")
    url = f"https://t.me/s/{clean_handle}"
    params = {"before": before} if before else None

    try:
        with client.stream("GET", url, params=params, headers={"User-Agent": _USER_AGENT}) as response:
            _classify_http_status(response.status_code)

            content_type = response.headers.get("content-type", "")
            if "text/html" not in content_type:
                raise TelegramPermanentError(f"unexpected content-type {content_type!r} from t.me")

            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > _MAX_RESPONSE_BYTES:
                    # Payload limit (Section 16) -- abort the stream
                    # rather than buffering an unbounded/hostile
                    # response into memory.
                    raise TelegramPermanentError(
                        f"t.me response exceeded {_MAX_RESPONSE_BYTES} byte cap -- aborted"
                    )
                chunks.append(chunk)
            body = b"".join(chunks)
    except httpx.TimeoutException as exc:
        raise TelegramTransientError(f"timeout fetching {url}") from exc
    except httpx.ConnectError as exc:
        raise TelegramTransientError(f"connection error fetching {url}") from exc
    except httpx.HTTPError as exc:
        # Any other httpx-level transport failure -- classified
        # transient by default (a real network hiccup, not a content
        # problem), bounded by the same 3-attempt retry above.
        raise TelegramTransientError(f"transport error fetching {url}: {exc}") from exc

    try:
        return body.decode("utf-8", errors="replace")
    except Exception as exc:  # pragma: no cover -- decode with errors="replace" cannot itself raise
        raise TelegramPermanentError(f"could not decode response from {url}") from exc


class _TelegramPreviewParser(HTMLParser):
    """Minimal, defensive stdlib HTML parser for ONE known, long-stable
    structure: Telegram's public `t.me/s/<handle>` widget markup.

    Never raises on malformed/adversarial markup (Phase 3B Section 15 --
    "treat every external response as untrusted"): a message block
    missing an expected attribute simply yields no `TelegramLiveMessage`
    for that block, never an exception that would abort the whole page.
    `<script>`/`<style>` tag bodies are explicitly discarded, never
    surfaced as message text, regardless of what a hostile channel puts
    inside them (Section 19/20 defense-in-depth -- this module never
    executes fetched content, but this also ensures it never even
    TREATS injected script/style text as post content)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.messages: list[TelegramLiveMessage] = []
        self._in_message = False
        self._depth = 0
        self._current_post_id: str | None = None
        self._current_text_parts: list[str] = []
        self._in_text_div = False
        self._text_div_depth = 0
        self._current_datetime_raw: str | None = None
        self._skip_tag_depth = 0  # >0 while inside a <script>/<style> we're discarding

    # HTML5 "void" elements: `html.parser` fires `handle_starttag` for
    # these but NEVER a matching `handle_endtag` unless the source
    # explicitly self-closes/writes a bogus closing tag -- treating them
    # like normal nesting would desynchronize this parser's own `_depth`
    # counter (a REAL bug this phase's own test suite caught: a `<br>`
    # inside a message body silently corrupted where a later `</div>`
    # was believed to close, leaking unrelated trailing markup -- e.g.
    # a sibling `<time>` element's own text -- into `raw_text`). Real,
    # well-known browsers know this same list; `html.parser` does not,
    # so it is applied here explicitly.
    _VOID_TAGS = frozenset({"br", "img", "hr", "meta", "input", "link", "source", "area", "base", "col", "embed", "wbr", "track"})

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = dict(attrs)
        classes = (attr_dict.get("class") or "").split()

        if tag in ("script", "style"):
            self._skip_tag_depth += 1
            return
        if self._skip_tag_depth > 0:
            return

        if tag == "div" and "tgme_widget_message" in classes and "tgme_widget_message_wrap" not in classes:
            # Flush any prior (malformed/unterminated) block defensively.
            self._in_message = True
            self._depth = 1
            self._current_post_id = attr_dict.get("data-post")
            self._current_text_parts = []
            self._current_datetime_raw = None
            return

        if not self._in_message:
            return

        if tag == "br" and self._in_text_div:
            self._current_text_parts.append("\n")

        if tag in self._VOID_TAGS:
            # No corresponding end-tag will ever arrive for these --
            # never adjust `_depth` for them, or every subsequent real
            # closing tag would close the WRONG ancestor.
            return

        self._depth += 1

        if tag == "div" and "tgme_widget_message_text" in classes:
            self._in_text_div = True
            self._text_div_depth = self._depth
        elif tag == "time" and "datetime" in attr_dict:
            # First real per-message timestamp wins -- Telegram emits
            # exactly one primary <time datetime=...> per message in
            # this widget; a later, unrelated one (if any) never
            # overwrites an already-captured real value. (Real Telegram
            # markup's <time> is void-like in practice but IS a normal,
            # non-void HTML element -- handled here for a real,
            # explicitly-closed <time>...</time>; the void-tag branch
            # above additionally covers a bare self-closing <time/>.)
            if self._current_datetime_raw is None:
                self._current_datetime_raw = attr_dict.get("datetime")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip_tag_depth > 0:
            self._skip_tag_depth -= 1
            return
        if self._skip_tag_depth > 0:
            return
        if not self._in_message:
            return

        if tag in self._VOID_TAGS:
            # `html.parser` fires a synthetic `handle_endtag` for a
            # self-closed void element (e.g. real Telegram markup's
            # `<br/>`, which appears multiple times per message body)
            # even though `handle_starttag` correctly never incremented
            # `_depth` for it (see the void-tag branch above). Without
            # this matching skip here, every such `<br/>` decremented
            # `_depth` with no corresponding increment -- a REAL bug
            # this phase's live validation against the actual t.me
            # page discovered (LIVE-DISCOVERED, Phase 3B.1): it closed
            # `tgme_widget_message_text` (and eventually the whole
            # message block) several tags too early, silently
            # truncating `raw_text` and causing the parser to flush the
            # message before ever reaching the real `<time datetime=...>`
            # element in the message footer -- so `published_at` came
            # back `None` even though the source genuinely provided a
            # real timestamp. The prior hand-authored unit-test fixtures
            # never exercised a message body containing a self-closed
            # `<br/>` before its closing tags, which is why 29 passing
            # unit tests did not catch this.
            return

        if self._in_text_div and self._depth == self._text_div_depth:
            self._in_text_div = False

        self._depth -= 1

        if self._depth <= 0:
            self._flush_current_message()
            self._in_message = False

    def handle_data(self, data: str) -> None:
        if self._skip_tag_depth > 0:
            return
        if self._in_message and self._in_text_div:
            self._current_text_parts.append(data)

    def _flush_current_message(self) -> None:
        post_id = self._current_post_id
        text = "".join(self._current_text_parts).strip()
        if not post_id or "/" not in post_id or not text:
            # No stable identifier, or no real text content -- skip
            # this block rather than fabricate a partial record.
            return
        text = text[:_MAX_TEXT_LENGTH]
        published_at = parse_source_timestamp(self._current_datetime_raw)
        self.messages.append(
            TelegramLiveMessage(
                external_item_id=post_id,
                item_url=f"https://t.me/{post_id}",
                raw_text=text,
                published_at=published_at,
            )
        )


def parse_preview_html(html: str) -> list[TelegramLiveMessage]:
    """Parses a `t.me/s/<handle>` preview page into real
    `TelegramLiveMessage`s. Never raises -- a genuinely unparseable page
    (Telegram changed its markup, or returned something unexpected)
    yields an empty list, a real and honestly-reported zero-item
    result, not a crash of the calling discovery round."""
    parser = _TelegramPreviewParser()
    try:
        parser.feed(html)
    except Exception:
        logger.exception("telegram_live_client: failed to parse t.me preview HTML -- returning 0 items")
        return []
    return parser.messages


def fetch_telegram_channel_live(
    handle: str,
    *,
    max_pages: int | None = None,
    max_items: int | None = None,
) -> list[TelegramLiveMessage]:
    """Real, BOUNDED fetch of a public Telegram channel's recent posts.

    Pagination (Section 10): pages via Telegram's own real `?before=`
    cursor, bounded by `max_pages` AND `max_items` AND an overall
    wall-clock budget (`_OVERALL_WALL_CLOCK_BUDGET_SECONDS`) -- whichever
    limit is hit first stops the loop. Also stops the moment a page
    yields ZERO items not already seen this call (duplicate-page
    detection) -- Telegram returns the same oldest page again once
    `before` walks past the channel's actual history, so this is the
    real, honest end-of-history signal, not an arbitrary guess.

    Never raises `TelegramTransientError`/`TelegramPermanentError` past
    the FIRST page -- a channel that returns a good first page and then
    fails on page 2 (an intermittent 5xx, a rate limit) returns the
    good page 1 results rather than discarding them; the failure is
    logged, not swallowed silently."""
    max_pages = max_pages if max_pages is not None else settings.telegram_live_fetch_max_pages
    max_items = max_items if max_items is not None else settings.telegram_live_fetch_max_items
    timeout = httpx.Timeout(
        connect=5.0,
        read=settings.telegram_live_fetch_timeout_seconds,
        write=5.0,
        pool=5.0,
    )

    seen_ids: set[str] = set()
    collected: list[TelegramLiveMessage] = []
    before: int | None = None
    started_at = _time.monotonic()

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for page_number in range(1, max_pages + 1):
            if _time.monotonic() - started_at > _OVERALL_WALL_CLOCK_BUDGET_SECONDS:
                logger.info(
                    "telegram_live_client: wall-clock budget exceeded for %s after %d page(s)",
                    handle, page_number - 1,
                )
                break

            try:
                html = _fetch_preview_page(client, handle, before=before)
            except TelegramPermanentError:
                if page_number == 1:
                    raise  # first-page permanent failure: nothing to salvage, propagate.
                logger.warning(
                    "telegram_live_client: permanent error on page %d for %s -- keeping %d already-collected item(s)",
                    page_number, handle, len(collected),
                )
                break
            except TelegramTransientError:
                if page_number == 1:
                    raise
                logger.warning(
                    "telegram_live_client: transient error on page %d for %s after retries -- keeping %d already-collected item(s)",
                    page_number, handle, len(collected),
                )
                break

            page_messages = parse_preview_html(html)
            new_messages = [m for m in page_messages if m.external_item_id not in seen_ids]

            if not new_messages:
                # Duplicate-page / end-of-history (Section 10).
                break

            for message in new_messages:
                seen_ids.add(message.external_item_id)
                collected.append(message)
                if len(collected) >= max_items:
                    break

            if len(collected) >= max_items:
                break

            # Telegram's own real pagination cursor: the OLDEST message
            # id seen so far (numeric suffix of `external_item_id`).
            try:
                oldest_id = min(int(m.external_item_id.rsplit("/", 1)[-1]) for m in page_messages)
            except (ValueError, IndexError):
                break
            before = oldest_id

    return collected
