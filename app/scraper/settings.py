BOT_NAME = "apex_scraper"

SPIDER_MODULES = ["app.scraper.spiders"]
NEWSPIDER_MODULE = "app.scraper.spiders"

# --- Twisted/asyncio bridge ---
# Scrapy's engine runs on Twisted. Our DB layer (SQLAlchemy async engine +
# asyncpg, in app/db/session.py) is asyncio-native, and it's the exact
# same engine/session factory the FastAPI app uses. Installing Twisted's
# asyncio-based reactor lets Scrapy natively schedule and await `async def`
# methods on spiders and pipelines (Scrapy wraps them with
# `deferred_from_coro`, which requires this reactor to be active) -- so
# `PostgresPipeline.open_spider`/`process_item` below can just
# `async with AsyncSessionLocal() as session:` directly. No separate sync
# engine, no manual `deferToThread` bridging. This has been Scrapy's
# default reactor for new projects since 2.7; set explicitly so it doesn't
# depend on the installed Scrapy version's default.
TWISTED_REACTOR = "twisted.internet.asyncioreactor.AsyncioSelectorReactor"

ITEM_PIPELINES = {
    "app.scraper.pipelines.PostgresPipeline": 300,
}

# --- Politeness / good-citizen crawling ---
ROBOTSTXT_OBEY = True
CONCURRENT_REQUESTS = 8
DOWNLOAD_DELAY = 0.5
AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_START_DELAY = 1
AUTOTHROTTLE_MAX_DELAY = 10

USER_AGENT = "ApexBot/0.1 (+https://apex.example.com/bot; contact: dev@apex.example.com)"

REQUEST_FINGERPRINTER_IMPLEMENTATION = "2.7"
FEED_EXPORT_ENCODING = "utf-8"

LOG_LEVEL = "INFO"
