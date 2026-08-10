import logging
from datetime import datetime

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models import Opportunity, Source
from app.models.enums import OpportunityType

logger = logging.getLogger(__name__)


class PostgresPipeline:
    """Persists scraped items into the `opportunities` table.

    Sync/async note: Scrapy's engine runs on Twisted; our DB layer is
    asyncio + SQLAlchemy async (the same `AsyncSessionLocal` the FastAPI
    app uses). This works without any sync-engine duplication or
    `deferToThread` bridging because `settings.py` installs Twisted's
    `AsyncioSelectorReactor` -- Scrapy then runs `async def` pipeline
    methods natively on that loop (via `deferred_from_coro`). If that
    reactor setting is ever removed, every `await` in this file breaks.
    """

    def __init__(self) -> None:
        self.source_id = None

    async def open_spider(self, spider) -> None:
        """Resolve (or create) this spider's Source row once per crawl run."""
        source_name = getattr(spider, "source_name", None)
        source_base_url = getattr(spider, "source_base_url", None)
        if not source_name or not source_base_url:
            raise ValueError(
                f"Spider {spider.name!r} must define `source_name` and "
                f"`source_base_url` class attributes for PostgresPipeline "
                f"to link scraped items to a Source row."
            )

        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Source).where(Source.name == source_name))
            source = result.scalar_one_or_none()

            if source is None:
                source = Source(name=source_name, base_url=source_base_url, active_status=True)
                session.add(source)
                await session.commit()
                await session.refresh(source)
                logger.info("Created new Source %r (%s)", source.name, source.id)
            else:
                logger.info("Using existing Source %r (%s)", source.name, source.id)

            self.source_id = source.id

    async def process_item(self, item, spider):
        data = dict(item)

        target_url = data.get("target_url")
        if not target_url:
            spider.logger.warning("Dropping item with no target_url: %r", data)
            return item

        try:
            opportunity_type = OpportunityType(data.get("opportunity_type"))
        except ValueError:
            spider.logger.warning(
                "Unknown opportunity_type %r on %s -- dropping item",
                data.get("opportunity_type"),
                target_url,
            )
            return item

        deadline = self._parse_deadline(data.get("deadline"), spider, target_url)

        async with AsyncSessionLocal() as session:
            # Exact-URL de-dupe within this source. This is deliberately
            # dumb (no fuzzy matching) -- semantic dedup via `embedding`
            # comes in Phase 2 (AI verification pipeline). This guard just
            # stops re-running a spider from creating duplicate rows.
            existing = await session.execute(
                select(Opportunity.id).where(
                    Opportunity.target_url == target_url,
                    Opportunity.source_id == self.source_id,
                )
            )
            if existing.scalar_one_or_none() is not None:
                spider.logger.debug("Skipping duplicate: %s", target_url)
                return item

            opportunity = Opportunity(
                title=(data.get("title") or "")[:500],
                description=data.get("description"),
                target_url=target_url,
                source_id=self.source_id,
                type=opportunity_type,
                deadline=deadline,
                eligibility_criteria=data.get("eligibility_criteria"),
                funding_details=data.get("funding_details"),
                location=data.get("location"),
                country=data.get("country"),
                is_verified=False,
                ai_confidence_score=None,
                raw_extracted_data=data,
                embedding=None,
            )
            session.add(opportunity)
            await session.commit()

        spider.logger.info("Saved opportunity: %s", data.get("title"))
        return item

    @staticmethod
    def _parse_deadline(raw, spider, target_url: str) -> datetime | None:
        """Items carry `deadline` as an ISO string (or None) since Scrapy
        Items are plain serializable data. asyncpg is strict about param
        types -- passing a str into a TIMESTAMP column raises a DataError
        at execute time -- so this must be a real datetime before it
        reaches the model."""
        if not raw:
            return None
        if isinstance(raw, datetime):
            return raw
        try:
            # A trailing "Z" (UTC) is only accepted by fromisoformat on
            # Python 3.11+; normalizing it here avoids depending on that.
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            spider.logger.warning("Could not parse deadline %r for %s", raw, target_url)
            return None
