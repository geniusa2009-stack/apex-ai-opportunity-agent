# Superseded

This placeholder described a planned top-level `scraper/` layout. As of
Step 1.3, the actual Scrapy project lives at `app/scraper/` instead (as a
subpackage of the `app` package), with `scrapy.cfg` at the repo root. That
layout is what lets the pipeline/spiders do
`from app.db.session import AsyncSessionLocal` and `from app.models import ...`
without any sys.path gymnastics -- see the root `README.md` for how to run it.

This directory is otherwise unused and can be deleted whenever convenient.
