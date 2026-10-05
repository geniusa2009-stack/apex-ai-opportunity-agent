"""Source connectors. Each one produces `IngestionEvent`s and nothing else.

Push connectors (webhooks) -- WhatsApp: `connectors/whatsapp.py`.
Pull connectors implement `SourceConnector` (`fetch()`):
* `RSSConnector`   -- RSS 2.0 / Atom feeds over plain HTTP (connectors/rss.py)
* the existing Telegram/Facebook adapters (app/services/source_adapters.py)
  are wrapped by `LegacyAdapterConnector`, so they feed the same pipeline
* volunteer submissions arrive through the API (app/api/v1/endpoints/agent.py)
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.agent.ingestion import IngestionEvent


@runtime_checkable
class SourceConnector(Protocol):
    name: str

    async def fetch(self) -> list[IngestionEvent]: ...
