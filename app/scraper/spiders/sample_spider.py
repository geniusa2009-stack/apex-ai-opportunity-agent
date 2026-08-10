from datetime import datetime, timezone

import scrapy

from app.models.enums import OpportunityType
from app.scraper.items import ScrapedOpportunityItem


class SampleOpportunitySpider(scrapy.Spider):
    """Demo spider against a public scraping sandbox (books.toscrape.com).

    This stands in for a real scholarship/internship source: a paginated
    listing page that links out to per-item detail pages -- the same
    shape as a scholarship board's "browse" page and per-scholarship
    detail page. It's a stable, ToS-friendly target for proving out
    pagination, detail-page following, and the DB pipeline end-to-end
    before pointing a spider at a real, more fragile scholarship site.

    Swapping in a real source later means changing: (1) start_urls,
    (2) the CSS selectors in parse()/parse_detail(), and (3) deriving a
    real `opportunity_type` from that source's own categories instead of
    the hardcoded placeholder below. The item schema and pipeline don't
    change.
    """

    name = "sample_opportunity"
    allowed_domains = ["books.toscrape.com"]
    start_urls = ["https://books.toscrape.com/"]

    # Read by PostgresPipeline.open_spider() to resolve/create the Source row.
    source_name = "Books ToScrape (Demo)"
    source_base_url = "https://books.toscrape.com/"

    custom_settings = {
        # Keep a manual test run short and predictable.
        "CLOSESPIDER_ITEMCOUNT": 40,
    }

    def parse(self, response):
        """Listing page: follow each item's detail link, then follow
        pagination ('next') to keep going."""
        for listing in response.css("article.product_pod"):
            detail_href = listing.css("h3 a::attr(href)").get()
            if detail_href:
                yield response.follow(detail_href, callback=self.parse_detail)

        next_href = response.css("li.next a::attr(href)").get()
        if next_href:
            yield response.follow(next_href, callback=self.parse)

    def parse_detail(self, response):
        """Detail page: extract the fields our Opportunity schema cares
        about and yield a ScrapedOpportunityItem for the pipeline."""
        title = response.css("div.product_main h1::text").get(default="").strip()
        description = response.css("#product_description ~ p::text").get(default="").strip()
        price = response.css("p.price_color::text").get(default="").strip()
        available = response.css("p.availability::text").re_first(r"\d+")

        item = ScrapedOpportunityItem()
        item["title"] = title
        item["description"] = description or None
        item["target_url"] = response.url
        # Demo placeholder: a real spider maps this from the source's own
        # taxonomy instead of hardcoding a single type for every item.
        item["opportunity_type"] = OpportunityType.SCHOLARSHIP.value
        item["deadline"] = None
        item["eligibility_criteria"] = f"{available} spots available" if available else None
        item["funding_details"] = f"Listed value: {price}" if price else None
        item["location"] = None
        item["country"] = None
        item["scraped_at"] = datetime.now(timezone.utc).isoformat()

        yield item
