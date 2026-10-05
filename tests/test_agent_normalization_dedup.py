"""Phase 3 -- deterministic normalization, date parsing and multi-layer
deduplication (no similarity-only DUPLICATE decisions)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import pytest_asyncio

from app.agent.dates import find_dates, parse_date
from app.agent.dedup import (
    DedupFacts,
    compare,
    decide,
    exact_matches,
    is_specific_url,
    pre_extraction_duplicate,
    record_fingerprints,
)
from app.agent.normalization import (
    content_hash,
    extract_links,
    is_url_shortener,
    is_valid_http_url,
    normalize_organization,
    normalize_text,
    normalize_title,
    normalize_url,
    registrable_domain,
    title_similarity,
)
from app.models.agent_enums import DedupOutcome
from app.models.enums import OpportunityType
from app.models.opportunity import Opportunity
from tests.agent_support import make_engine_and_session_factory, make_source

# --------------------------------------------------------------------------
# URL normalization
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://www.DAAD.de/en/scholarships/?utm_source=wa&utm_medium=group", "https://daad.de/en/scholarships"),
        ("http://daad.de/en/scholarships/", "https://daad.de/en/scholarships"),
        ("https://daad.de:443/en//scholarships#apply", "https://daad.de/en/scholarships"),
        ("www.example.org/apply?b=2&a=1&fbclid=XYZ", "https://example.org/apply?a=1&b=2"),
        ("https://example.org/apply?gclid=1&si=abc&id=7", "https://example.org/apply?id=7"),
        ("(https://example.org/apply).", "https://example.org/apply"),
        ("https://example.org/apply،", "https://example.org/apply"),
        ("https://l.facebook.com/l.php?u=https%3A%2F%2Fexample.org%2Fapply%3Futm_source%3Dfb&h=AT0", "https://example.org/apply"),
        ("https://www.google.com/url?q=https://example.org/apply&sa=D", "https://example.org/apply"),
        ("https://example.org:8080/x", "https://example.org:8080/x"),
        ("https://en.wikipedia.org/wiki/Fulbright_(program)", "https://en.wikipedia.org/wiki/Fulbright_(program)"),
    ],
)
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "not a url", "ftp://example.org/file", "javascript:alert(1)", "https://localhost/x"])
def test_normalize_url_rejects_invalid(raw):
    assert normalize_url(raw) is None


def test_tracking_only_differences_collapse_to_one_key():
    a = normalize_url("https://forms.gle/AbC123?utm_campaign=x")
    b = normalize_url("http://forms.gle/AbC123/?fbclid=y&igshid=z")
    assert a == b


def test_extract_links_cleans_and_dedupes():
    text = "قدم هنا: https://example.org/apply. أو www.example.org/apply و (https://other.org/x)!"
    assert extract_links(text) == ["https://example.org/apply", "https://www.example.org/apply", "https://other.org/x"]


def test_url_helpers():
    assert is_url_shortener("https://bit.ly/abc") and is_url_shortener("bit.ly/abc")
    assert not is_url_shortener("https://daad.de/x")
    assert is_valid_http_url("https://daad.de/x") and not is_valid_http_url("https://localhost/x")
    assert not is_valid_http_url("daad.de") and not is_valid_http_url("https://exa mple.org")
    assert registrable_domain("https://www.scholar.eun.eg/apply") == "scholar.eun.eg"
    assert registrable_domain("https://apply.cam.ac.uk/x") == "cam.ac.uk"
    assert registrable_domain("https://portal.daad.de/x") == "daad.de"


def test_specific_url():
    assert not is_specific_url("https://daad.de")
    assert not is_specific_url("https://daad.de/en")
    assert is_specific_url("https://daad.de/en/scholarships")
    assert is_specific_url("https://example.org?id=3")


# --------------------------------------------------------------------------
# Text / title normalization and hashing
# --------------------------------------------------------------------------


def test_arabic_normalization_folds_variants_and_digits():
    assert normalize_text("أَهْلاً  بِكُمْ ــ إلى منحةٍ") == normalize_text("اهلا بكم الى منحه")
    assert normalize_text("آخر موعد ١٥/١٠/٢٠٢٦") == "اخر موعد 15/10/2026"
    assert normalize_text("Ｆｕｌｌｙ​ Funded") == "fully funded"


def test_title_normalization_drops_decoration_but_keeps_identity():
    assert normalize_title("🔥 عاجل: *منحة DAAD للماجستير 2027* 🔥") == normalize_title("منحة daad للماجستير 2027")
    assert "2027" in normalize_title("Scholarship 2027")
    assert normalize_title("NEW!! Fulbright Program") == "fulbright program"


def test_title_similarity_and_org_normalization():
    assert title_similarity("DAAD Masters Scholarship 2027", "Scholarship DAAD Masters 2027") == 1.0
    assert title_similarity("DAAD Masters Scholarship", "Chevening Scholarship") < 0.5
    assert title_similarity(None, "x") == 0.0
    assert normalize_organization("The DAAD Foundation, Inc.") == "daad"


def test_content_hash_is_stable_across_forwarding_noise():
    a = "*منحة DAAD* 🔥\nقدم: https://daad.de/apply?utm_source=wa"
    b = "Forwarded\n منحة   DAAD\nقدم: http://www.daad.de/apply/?fbclid=1 "
    assert content_hash(a) == content_hash(b)
    assert content_hash(a) != content_hash(a + " deadline 2026-10-15")
    assert content_hash("") is None and content_hash("  🔥 ") is None


# --------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Deadline: 2026-10-15", [dt.date(2026, 10, 15)]),
        ("آخر موعد 15/10/2026", [dt.date(2026, 10, 15)]),
        ("آخر موعد ١٥-١٠-٢٠٢٦", [dt.date(2026, 10, 15)]),
        ("آخر موعد للتقديم: 15 أكتوبر 2026", [dt.date(2026, 10, 15)]),
        ("ينتهي 1 تشرين الثاني 2026", [dt.date(2026, 11, 1)]),
        ("Apply by October 15, 2026 or 1st Nov 2026", [dt.date(2026, 10, 15), dt.date(2026, 11, 1)]),
        ("starts 31/02/2026", []),  # invalid date
        ("in October", []),  # not a full date
        ("year 1999-01-01", []),
    ],
)
def test_find_dates(text, expected):
    assert find_dates(text) == expected


def test_parse_date():
    assert parse_date("2026-10-15") == dt.date(2026, 10, 15)
    assert parse_date("2026-10-15T23:59:00Z") == dt.date(2026, 10, 15)
    assert parse_date("15 October 2026") == dt.date(2026, 10, 15)
    assert parse_date("October 2026") is None
    assert parse_date("15/10/2026 or 20/10/2026") is None  # ambiguous
    assert parse_date(None) is None


# --------------------------------------------------------------------------
# compare() -- pure decision rules
# --------------------------------------------------------------------------

BASE = dict(
    title="DAAD Master's Scholarship in Germany 2027",
    organization="DAAD",
    opportunity_type="scholarship",
    deadline=dt.date(2026, 10, 15),
    application_url="https://www.daad.de/en/study-and-research-in-germany/scholarships/",
)


def facts(**overrides) -> DedupFacts:
    data = dict(BASE)
    data.update(overrides)
    return DedupFacts(**data)


def test_same_application_url_is_duplicate():
    result = compare(facts(application_url=BASE["application_url"] + "?utm_source=x"), facts())
    assert result.outcome == DedupOutcome.DUPLICATE
    assert result.signals["application_url_equal"]


def test_same_application_url_new_deadline_is_update():
    result = compare(facts(deadline=dt.date(2026, 11, 1)), facts())
    assert result.outcome == DedupOutcome.UPDATE_EXISTING
    assert any("deadline changed" in r for r in result.reasons)


def test_same_title_and_org_is_duplicate():
    result = compare(facts(application_url=None, title="🔥 DAAD Master's Scholarship in Germany 2027"), facts(application_url=None))
    assert result.outcome == DedupOutcome.DUPLICATE


def test_new_message_filling_missing_deadline_is_update():
    result = compare(facts(), facts(deadline=None))
    assert result.outcome == DedupOutcome.UPDATE_EXISTING


def test_same_title_unknown_org_needs_human():
    result = compare(facts(organization=None, application_url=None, deadline=None), facts(organization=None, application_url=None, deadline=None))
    assert result.outcome == DedupOutcome.POSSIBLE_DUPLICATE


def test_different_edition_year_is_not_a_duplicate():
    result = compare(facts(title="Chevening Scholarship 2027", application_url=None), facts(title="Chevening Scholarship 2026", application_url=None))
    assert result.outcome == DedupOutcome.POSSIBLE_DUPLICATE  # same program, maybe new cycle -> human
    unrelated = compare(facts(title="Erasmus Mundus 2027", application_url=None, organization="EU"), facts(title="Chevening 2026", application_url=None, organization="FCDO"))
    assert unrelated.outcome == DedupOutcome.NEW


def test_organization_conflict_blocks_duplicate():
    result = compare(facts(organization="Fulbright Commission"), facts())
    assert result.outcome == DedupOutcome.POSSIBLE_DUPLICATE


def test_similarity_alone_never_makes_a_duplicate():
    # Very similar titles, nothing else in common -> at most POSSIBLE_DUPLICATE.
    new = DedupFacts(title="DAAD Masters Scholarship Germany 2027 fully funded")
    old = DedupFacts(title="DAAD Masters Scholarship Germany 2027")
    assert compare(new, old).outcome in (DedupOutcome.POSSIBLE_DUPLICATE, DedupOutcome.NEW)
    assert compare(new, old).outcome != DedupOutcome.DUPLICATE


def test_generic_homepage_url_is_not_identity():
    a = DedupFacts(title="DAAD Summer School", organization="DAAD", official_url="https://www.daad.de/")
    b = DedupFacts(title="DAAD Research Grants for Doctoral Candidates", organization="DAAD", official_url="https://daad.de")
    assert compare(a, b).outcome == DedupOutcome.NEW


def test_unrelated_is_new():
    assert compare(DedupFacts(title="Google STEP internship", organization="Google"), facts()).outcome == DedupOutcome.NEW


def test_content_hash_equal_is_duplicate():
    assert compare(DedupFacts(content_hash="a" * 64), DedupFacts(content_hash="a" * 64)).outcome == DedupOutcome.DUPLICATE


# --------------------------------------------------------------------------
# DB layers
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session():
    engine, factory = await make_engine_and_session_factory()
    async with factory() as db:
        yield db
    await engine.dispose()


async def _opportunity(db, *, title, deadline=None, target_url="https://daad.de/en/study-and-research-in-germany/scholarships", source=None):
    source = source or await make_source(db, source_type="website")
    opp = Opportunity(
        title=title,
        target_url=target_url,
        source_id=source.id,
        type=OpportunityType.SCHOLARSHIP,
        deadline=dt.datetime.combine(deadline, dt.time(23, 59), tzinfo=dt.timezone.utc) if deadline else None,
    )
    db.add(opp)
    await db.flush()
    return opp


@pytest.mark.asyncio
class TestDedupLayers:
    async def test_layer1_url_and_layer2_application_url_exact(self, session):
        opp = await _opportunity(session, title=BASE["title"], deadline=BASE["deadline"])
        await record_fingerprints(session, opp.id, facts(target_url=opp.target_url))
        matches = await exact_matches(session, facts(application_url=BASE["application_url"] + "?utm_medium=wa"))
        assert set(matches[opp.id]) >= {"application_url", "url"}

    async def test_layer3_title_and_layer4_content_hash(self, session):
        opp = await _opportunity(session, title=BASE["title"])
        await record_fingerprints(session, opp.id, facts(application_url=None, content_hash="b" * 64))
        assert "title" in (await exact_matches(session, DedupFacts(title="🔥 " + BASE["title"])))[opp.id]
        assert "content_hash" in (await exact_matches(session, DedupFacts(content_hash="b" * 64)))[opp.id]

    async def test_record_fingerprints_is_idempotent(self, session):
        opp = await _opportunity(session, title=BASE["title"])
        first = await record_fingerprints(session, opp.id, facts())
        assert first > 0
        assert await record_fingerprints(session, opp.id, facts()) == 0

    async def test_decide_new_when_nothing_matches(self, session):
        decision = await decide(session, facts())
        assert decision.outcome == DedupOutcome.NEW and decision.opportunity_id is None

    async def test_decide_duplicate_via_application_url(self, session):
        opp = await _opportunity(session, title=BASE["title"], deadline=BASE["deadline"])
        await record_fingerprints(session, opp.id, facts())
        decision = await decide(session, facts(title="Germany DAAD scholarship for masters 2027"))
        assert decision.outcome == DedupOutcome.DUPLICATE and decision.opportunity_id == opp.id
        assert decision.candidates[0]["exact_layers"]

    async def test_decide_update_existing_on_extended_deadline(self, session):
        opp = await _opportunity(session, title=BASE["title"], deadline=BASE["deadline"])
        await record_fingerprints(session, opp.id, facts())
        decision = await decide(session, facts(deadline=dt.date(2026, 11, 30)))
        assert decision.outcome == DedupOutcome.UPDATE_EXISTING and decision.opportunity_id == opp.id

    async def test_semantic_candidate_only_yields_possible_duplicate_at_most(self, session):
        opp = await _opportunity(session, title="Fully funded masters program in Germany", target_url="https://uni.example.de/program")

        async def fake_semantic(_facts):
            return {opp.id: 0.97}  # embeddings say "almost identical"

        decision = await decide(
            session,
            DedupFacts(title="Fully funded master program Germany", organization="Some University"),
            semantic_search=fake_semantic,
        )
        assert decision.outcome in (DedupOutcome.POSSIBLE_DUPLICATE, DedupOutcome.NEW)
        assert decision.outcome != DedupOutcome.DUPLICATE
        assert decision.candidates[0]["semantic_score"] == 0.97

    async def test_semantic_candidate_with_conflicting_org_stays_new(self, session):
        opp = await _opportunity(session, title="Leadership fellowship 2027", target_url="https://a.example.org/f")
        await record_fingerprints(session, opp.id, DedupFacts(title="Leadership fellowship 2027", organization="Org A"))

        async def fake_semantic(_facts):
            return {opp.id: 0.99}

        decision = await decide(session, DedupFacts(title="Leadership fellowship 2027", organization="Org B"), semantic_search=fake_semantic)
        assert decision.outcome == DedupOutcome.POSSIBLE_DUPLICATE  # never auto-merged across organizations

    async def test_pre_extraction_duplicate_skips_llm_only_without_new_dates(self, session):
        opp = await _opportunity(session, title=BASE["title"], deadline=BASE["deadline"])
        await record_fingerprints(session, opp.id, facts())
        links = [BASE["application_url"] + "?utm_source=wa"]
        same = await pre_extraction_duplicate(session, links=links, text="منحة DAAD آخر موعد 15/10/2026", content_hash="c" * 64)
        assert same is not None and same.outcome == DedupOutcome.DUPLICATE and same.opportunity_id == opp.id
        extended = await pre_extraction_duplicate(session, links=links, text="تم مد آخر موعد إلى 30/11/2026", content_hash="d" * 64)
        assert extended is None  # new date -> must go through extraction (maybe UPDATE_EXISTING)
        generic = await pre_extraction_duplicate(session, links=["https://daad.de"], text="x", content_hash="e" * 64)
        assert generic is None  # a homepage link proves nothing
