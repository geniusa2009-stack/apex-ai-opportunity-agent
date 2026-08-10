import scrapy


class ScrapedOpportunityItem(scrapy.Item):
    """Raw shape of a scraped listing, before AI verification/cleanup.

    Field names deliberately mirror the subset of `Opportunity` columns
    fillable directly from scraping. `dict(item)` is what gets stored
    verbatim into `Opportunity.raw_extracted_data` (JSONB) by the
    pipeline, so the original scrape is never lost even if parsing logic
    or the extraction schema changes later (Phase 2 re-derives cleaned
    fields from this raw payload via the AI verification step).
    """

    title = scrapy.Field()
    description = scrapy.Field()
    target_url = scrapy.Field()
    opportunity_type = scrapy.Field()   # must match an app.models.enums.OpportunityType value
    deadline = scrapy.Field()           # ISO 8601 string, or None
    eligibility_criteria = scrapy.Field()
    funding_details = scrapy.Field()
    location = scrapy.Field()
    country = scrapy.Field()
    scraped_at = scrapy.Field()         # ISO 8601 timestamp, set by the spider
