"""Phase 5 -- official-source verification on top of the existing D6
Claim/Evidence system. Pages are served by fakes / httpx.MockTransport:
no real network."""

from __future__ import annotations

import datetime as dt

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.agent.extraction import ExtractedOpportunity
from app.agent.verification import (
    FetchedPage,
    HttpPageFetcher,
    NullPageFetcher,
    deadline_dates_on_page,
    html_to_text,
    message_red_flags,
    verify_opportunity,
)
from app.models.agent_enums import VerificationLevel
from app.models.claim import Claim
from app.models.enums import OpportunityType
from app.models.evidence import Evidence
from app.models.opportunity import Opportunity
from tests.agent_support import SAMPLE_OPPORTUNITY_TEXT, make_engine_and_session_factory, make_source, sample_extraction

TODAY = dt.date(2026, 10, 5)
APP_URL = "https://www.daad.de/en/study-and-research-in-germany/scholarships/"

OFFICIAL_HTML = """
<html><head><title>DAAD Scholarships: Master's studies in Germany</title>
<script>var tracking = "2026-01-01";</script></head>
<body><h1>DAAD Master's Scholarship in Germany 2027</h1>
<p>The German Academic Exchange Service (DAAD) offers fully funded scholarships.</p>
<p>Application deadline: 15 October 2026</p></body></html>
"""


class FakeFetcher:
    def __init__(self, page: FetchedPage):
        self.page = page
        self.urls: list[str] = []

    async def fetch(self, url: str) -> FetchedPage:
        self.urls.append(url)
        self.page.url = url
        return self.page


def page(html=OFFICIAL_HTML, status=200, final_url=APP_URL, error=None) -> FetchedPage:
    title, text = html_to_text(html)
    return FetchedPage(url=APP_URL, final_url=final_url, status_code=status, title=title, text=text, error=error)


@pytest_asyncio.fixture
async def ctx():
    engine, factory = await make_engine_and_session_factory()
    async with factory() as db:
        source = await make_source(db, source_type="whatsapp", trust_score=0.4)
        opp = Opportunity(title="DAAD", target_url=APP_URL, source_id=source.id, type=OpportunityType.SCHOLARSHIP)
        db.add(opp)
        await db.flush()
        yield db, source, opp
    await engine.dispose()


async def run(ctx, fetcher, **extraction_overrides):
    db, source, opp = ctx
    extraction = ExtractedOpportunity.model_validate(sample_extraction(**extraction_overrides))
    return await verify_opportunity(
        db,
        opportunity_id=opp.id,
        extraction=extraction,
        message_text=SAMPLE_OPPORTUNITY_TEXT,
        source_id=source.id,
        source_type="whatsapp",
        trust_score=source.trust_score,
        fetcher=fetcher,
        today=TODAY,
    )


@pytest.mark.asyncio
class TestVerification:
    async def test_official_page_confirms_facts_and_evidence_is_stored(self, ctx):
        report = await run(ctx, FakeFetcher(page()))
        assert report.level == VerificationLevel.OFFICIAL_CONFIRMED
        assert {"title", "organization", "deadline"} <= set(report.confirmed_claims)
        assert not report.blocking and not report.expired
        db = ctx[0]
        claims = {c.claim_type: c for c in (await db.execute(select(Claim))).scalars()}
        assert {"title", "organization", "deadline", "application_url", "funding"} <= set(claims)
        evidence = (await db.execute(select(Evidence))).scalars().all()
        official = [e for e in evidence if e.source_type == "official_page"]
        assert official and all(e.source_url == APP_URL for e in official)
        assert report.d6_decision == "verified"

    async def test_contradicting_deadline_on_official_page(self, ctx):
        html = OFFICIAL_HTML.replace("15 October 2026", "30 September 2026")
        report = await run(ctx, FakeFetcher(page(html)))
        assert report.level == VerificationLevel.CONTRADICTED
        assert "deadline" in report.contradicted_claims and report.blocking

    async def test_closed_applications_notice_is_a_contradiction(self, ctx):
        html = OFFICIAL_HTML.replace("Application deadline", "Applications are now closed. Deadline was")
        report = await run(ctx, FakeFetcher(page(html)))
        assert report.level == VerificationLevel.CONTRADICTED
        assert "open_for_applications" in report.contradicted_claims

    async def test_dead_link_is_a_contradiction(self, ctx):
        report = await run(ctx, FakeFetcher(page(status=404, error="HTTP 404")))
        assert report.level == VerificationLevel.CONTRADICTED and report.blocking

    async def test_unreachable_page_degrades_to_source_only_not_verified(self, ctx):
        report = await run(ctx, FakeFetcher(FetchedPage(url=APP_URL, error="timeout")))
        assert report.level == VerificationLevel.SOURCE_ONLY
        assert any(c.name == "official_page_reachable" and c.status == "warn" for c in report.checks)

    async def test_weak_source_without_any_link_is_blocking(self, ctx):
        report = await run(ctx, NullPageFetcher(), application_url=None, official_url=None)
        assert report.level == VerificationLevel.SOURCE_ONLY
        assert report.blocking  # low-trust + nothing to check -> human must look closer
        assert "no application or official link" in report.red_flags

    async def test_trusted_source_without_link_is_not_blocking(self, ctx):
        db, source, opp = ctx
        source.trust_score = 0.9
        report = await run(ctx, NullPageFetcher(), application_url=None, official_url=None)
        assert report.level == VerificationLevel.SOURCE_ONLY and not report.blocking

    async def test_expired_opportunity_is_rejected(self, ctx):
        report = await run(ctx, FakeFetcher(page()), deadline=None)
        assert not report.expired
        db, source, opp = ctx
        extraction = ExtractedOpportunity.model_validate(sample_extraction())
        report = await verify_opportunity(db, opportunity_id=opp.id, extraction=extraction,
                                          message_text=SAMPLE_OPPORTUNITY_TEXT, source_id=source.id,
                                          source_type="whatsapp", trust_score=0.4, fetcher=FakeFetcher(page()),
                                          today=dt.date(2026, 10, 20))
        assert report.expired and report.level == VerificationLevel.REJECTED

    async def test_cross_site_redirect_is_flagged(self, ctx):
        report = await run(ctx, FakeFetcher(page(final_url="https://totally-other-site.example/landing")))
        assert any("redirects" in f for f in report.red_flags)

    async def test_shortened_link_is_not_followed(self, ctx):
        fetcher = FakeFetcher(page())
        text_links_short = "https://bit.ly/daad-x"
        db, source, opp = ctx
        extraction = ExtractedOpportunity.model_validate(sample_extraction(application_url=text_links_short))
        report = await verify_opportunity(db, opportunity_id=opp.id, extraction=extraction, message_text="x " + text_links_short,
                                          source_id=source.id, source_type="whatsapp", trust_score=0.4,
                                          fetcher=fetcher, today=TODAY)
        assert fetcher.urls == []
        assert any("shortened link" in f for f in report.red_flags)


def test_scam_signals_are_flagged():
    extraction = ExtractedOpportunity.model_validate(sample_extraction(requires_payment=True))
    flags = message_red_flags(extraction, "قبول مضمون 100% ادفع 500 جنيه على فودافون كاش")
    assert "applicants are asked to pay a fee" in flags
    assert any("payment request" in f for f in flags)
    assert "promises guaranteed acceptance" in flags


def test_deadline_dates_near_keywords_only():
    text = "Founded 2020-01-01. Event on 1 March 2027. Application deadline: 15 October 2026."
    assert deadline_dates_on_page(text) == [dt.date(2026, 10, 15)]


def test_html_to_text_ignores_scripts():
    title, text = html_to_text(OFFICIAL_HTML)
    assert title.startswith("DAAD Scholarships")
    assert "tracking" not in text and "15 October 2026" in text


@pytest.mark.asyncio
class TestHttpPageFetcher:
    async def test_fetches_html_and_extracts_text(self):
        def handler(request: httpx.Request):
            assert "APEXOpportunityVerifier" in request.headers["user-agent"]
            return httpx.Response(200, html=OFFICIAL_HTML)

        result = await HttpPageFetcher(transport=httpx.MockTransport(handler)).fetch(APP_URL)
        assert result.ok and "Application deadline" in result.text

    @pytest.mark.parametrize("url", ["http://127.0.0.1/admin", "http://localhost:8000/x", "http://10.0.0.5/x",
                                     "http://[::1]/x", "http://metadata.internal/x"])
    async def test_refuses_private_hosts(self, url):
        result = await HttpPageFetcher(transport=httpx.MockTransport(lambda r: httpx.Response(200))).fetch(url)
        assert not result.ok and "refused" in result.error

    async def test_refuses_non_html_and_reports_http_errors(self):
        pdf = await HttpPageFetcher(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"}))).fetch(APP_URL)
        assert "unsupported content type" in pdf.error
        missing = await HttpPageFetcher(transport=httpx.MockTransport(lambda r: httpx.Response(404))).fetch(APP_URL)
        assert missing.status_code == 404 and not missing.ok

    async def test_size_cap(self):
        big = "<html><body>" + ("x" * 5000) + "</body></html>"
        result = await HttpPageFetcher(max_bytes=1000, transport=httpx.MockTransport(
            lambda r: httpx.Response(200, html=big))).fetch(APP_URL)
        assert len(result.text) <= 1000

    async def test_timeout_is_reported_not_raised(self):
        def handler(request):
            raise httpx.ReadTimeout("slow")

        result = await HttpPageFetcher(transport=httpx.MockTransport(handler)).fetch(APP_URL)
        assert result.error == "timeout"
