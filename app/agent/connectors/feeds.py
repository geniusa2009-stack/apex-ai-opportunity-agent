"""Pull connectors: RSS/Atom feeds and a bridge for the existing source
adapters (Telegram public preview, Facebook Graph).

Both only produce `IngestionEvent`s; the pipeline logic is not duplicated.
Only feeds/sources that are configured by the team are polled -- nothing is
discovered or scraped beyond them.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from email.utils import parsedate_to_datetime
from typing import Any

import defusedxml.ElementTree as SafeET  # XML from the internet: never the stdlib parser
import httpx

from app.agent.errors import AgentError, TransientError, category_for_http_status
from app.agent.ingestion import IngestionEvent
from app.agent.normalization import normalize_text
from app.agent.verification import USER_AGENT, html_to_text
from app.models.enums import SourceType

_ATOM = "{http://www.w3.org/2005/Atom}"


def _parse_date(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def parse_feed(xml_text: str) -> list[dict[str, Any]]:
    """RSS 2.0 and Atom -> [{id, title, link, text, published_at}]."""
    root = SafeET.fromstring(xml_text)
    items: list[dict[str, Any]] = []
    for item in root.iter("item"):  # RSS 2.0
        link = (item.findtext("link") or "").strip() or None
        guid = (item.findtext("guid") or "").strip() or link
        _, description = html_to_text(item.findtext("description") or "")
        items.append({"id": guid, "title": (item.findtext("title") or "").strip(), "link": link,
                      "text": description, "published_at": _parse_date(item.findtext("pubDate"))})
    for entry in root.iter(f"{_ATOM}entry"):  # Atom
        link_el = entry.find(f"{_ATOM}link")
        link = link_el.get("href") if link_el is not None else None
        _, summary = html_to_text(entry.findtext(f"{_ATOM}summary") or entry.findtext(f"{_ATOM}content") or "")
        items.append({"id": (entry.findtext(f"{_ATOM}id") or link or "").strip() or None,
                      "title": (entry.findtext(f"{_ATOM}title") or "").strip(), "link": link, "text": summary,
                      "published_at": _parse_date(entry.findtext(f"{_ATOM}updated") or entry.findtext(f"{_ATOM}published"))})
    return items


class RSSConnector:
    name = "rss"

    def __init__(self, feed_url: str, *, source_type: SourceType = SourceType.RSS, max_items: int = 30,
                 timeout_seconds: float = 15.0, transport: httpx.AsyncBaseTransport | None = None):
        self.feed_url = feed_url
        self.source_type = source_type
        self.max_items = max_items
        self._timeout = timeout_seconds
        self._transport = transport

    async def fetch(self) -> list[IngestionEvent]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True, transport=self._transport,
                                         headers={"User-Agent": USER_AGENT}) as client:
                response = await client.get(self.feed_url)
        except (httpx.TimeoutException, httpx.NetworkError, asyncio.TimeoutError) as exc:
            raise TransientError(f"feed unreachable: {type(exc).__name__}", code="FEED_UNREACHABLE") from exc
        if response.status_code >= 400:
            raise AgentError(f"feed HTTP {response.status_code}", code=f"FEED_HTTP_{response.status_code}",
                             category=category_for_http_status(response.status_code))
        events: list[IngestionEvent] = []
        for item in parse_feed(response.text)[: self.max_items]:
            text = "\n".join(filter(None, [item["title"], item["text"], item["link"]]))
            if not normalize_text(text):
                continue
            events.append(IngestionEvent(
                source_type=self.source_type,
                source_key=self.feed_url,
                source_base_url=self.feed_url,
                external_message_id=(item["id"] or item["link"] or item["title"])[:255],
                text=text,
                links=[item["link"]] if item["link"] else [],
                received_at=item["published_at"],
            ))
        return events


class LegacyAdapterConnector:
    """Wraps an existing `app.services.source_adapters` adapter (sync
    `fetch(config)` returning `RawDiscoveryResult`s) so its items enter the
    agent pipeline instead of the old direct-upsert path."""

    def __init__(self, adapter: Any, config: Any, *, source_type: SourceType, source_key: str, base_url: str = ""):
        self.name = f"legacy:{getattr(adapter, 'source_kind', type(adapter).__name__)}"
        self._adapter = adapter
        self._config = config
        self._source_type = source_type
        self._source_key = source_key
        self._base_url = base_url

    async def fetch(self) -> list[IngestionEvent]:
        results = await asyncio.to_thread(self._adapter.fetch, self._config)
        return [
            IngestionEvent(
                source_type=self._source_type,
                source_key=self._source_key,
                source_base_url=self._base_url,
                external_message_id=r.external_item_id,
                text=r.raw_text,
                links=[r.item_url] if r.item_url else [],
                received_at=r.published_at or r.retrieved_at,
            )
            for r in results
        ]
