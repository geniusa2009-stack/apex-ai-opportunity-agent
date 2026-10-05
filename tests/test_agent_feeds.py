"""Pull connectors (RSS/Atom + existing source adapters) produce the same
IngestionEvent as every other source."""

from __future__ import annotations

import httpx
import pytest

from app.agent.connectors import SourceConnector
from app.agent.connectors.feeds import LegacyAdapterConnector, RSSConnector, parse_feed
from app.agent.errors import ErrorCategory
from app.models.enums import SourceType
from app.services.source_adapters import RawDiscoveryResult

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Opps</title>
<item><title>Fulbright 2027</title><link>https://example.org/fulbright</link><guid>g-1</guid>
<description>&lt;p&gt;Deadline: 1 March 2027&lt;/p&gt;</description><pubDate>Mon, 05 Oct 2026 09:00:00 GMT</pubDate></item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Hackathon</title><link href="https://example.org/hack"/><id>urn:1</id>
<updated>2026-10-01T10:00:00Z</updated><summary>Apply now</summary></entry></feed>"""

EVIL = """<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;">]>
<rss><channel><item><title>&lol2;</title></item></channel></rss>"""


def test_parse_rss_and_atom():
    [rss] = parse_feed(RSS)
    assert rss["id"] == "g-1" and rss["link"] == "https://example.org/fulbright"
    assert "Deadline: 1 March 2027" in rss["text"] and rss["published_at"].year == 2026
    [atom] = parse_feed(ATOM)
    assert atom["link"] == "https://example.org/hack" and atom["id"] == "urn:1"


def test_xml_entity_expansion_is_refused():
    with pytest.raises(Exception):
        parse_feed(EVIL)


@pytest.mark.asyncio
async def test_rss_connector_produces_ingestion_events():
    connector = RSSConnector("https://example.org/feed.xml", transport=httpx.MockTransport(lambda r: httpx.Response(200, text=RSS)))
    assert isinstance(connector, SourceConnector)
    [event] = await connector.fetch()
    assert event.source_type == SourceType.RSS and event.external_message_id == "g-1"
    assert event.links == ["https://example.org/fulbright"]


@pytest.mark.asyncio
async def test_rss_connector_errors_are_classified():
    connector = RSSConnector("https://example.org/feed.xml", transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(Exception) as info:
        await connector.fetch()
    assert info.value.category == ErrorCategory.EXTERNAL_SERVICE


@pytest.mark.asyncio
async def test_legacy_adapter_bridge():
    class FakeAdapter:
        source_kind = "telegram_channel"

        def fetch(self, config):
            return [RawDiscoveryResult(raw_text="منحة جديدة", source_kind="telegram_channel", external_item_id="55",
                                       item_url="https://t.me/chan/55")]

    connector = LegacyAdapterConnector(FakeAdapter(), object(), source_type=SourceType.TELEGRAM, source_key="@chan")
    [event] = await connector.fetch()
    assert event.source_type == SourceType.TELEGRAM and event.external_message_id == "55"
    assert event.links == ["https://t.me/chan/55"]
