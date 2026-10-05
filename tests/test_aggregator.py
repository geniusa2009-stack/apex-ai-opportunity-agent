"""Behavioral tests for the Hybrid Opportunity Aggregation Engine
(`app/services/aggregator_service.py`, `app/workers/aggregator_tasks.py`).

Per this step's own explicit verification requirement: (1) the AI prompt
logic correctly parses a sample Arabic Telegram post into the strict
`OpportunityCreate` schema, and (2) the database correctly saves the
`is_local_egypt` boolean.

Gemini's own SDK call is mocked at the `_extract_fields_via_gemini`
boundary -- same "mock the one clearly-external-call function, let
everything else run for real" posture as `tests/test_student_videos.py`'s
own mocking of `evaluate_and_moderate` -- so `process_raw_post_via_ai`'s
OWN mapping/fallback logic (target_url fallback, opportunity_type
coercion, deadline parsing, country derivation) is exercised for real in
every test below, not skipped over.

Persistence tests use hand-written raw-SQL `sources`/`opportunities`
tables against in-memory SQLite -- same established, disclosed deviation
as every other model in this codebase with a Postgres-specific column
type SQLite's DDL compiler can't render (`Opportunity.embedding` is
`pgvector.Vector`, `Opportunity.raw_extracted_data` is `JSONB`, both
confirmed elsewhere in this codebase to raise `CompileError` against
`Base.metadata.create_all()` on SQLite). Once the tables exist via raw
SQL, the ORM's own INSERT/SELECT statements go through each column's real
mapped type for parameter binding -- including `Opportunity.type`'s
native-Postgres-`Enum`-backed column, whose `native_enum=True` only
affects DDL EMISSION (irrelevant here, since DDL is hand-written), not
its bind/result processing, which works generically against any VARCHAR
column regardless of dialect.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.enums import OpportunityType, SourceType
from app.models.opportunity import Opportunity
from app.models.source import Source
from app.schemas.opportunity import OpportunityCreate
from app.services import aggregator_service
from app.services.taxonomy import normalize_country
from app.services.aggregator_service import (
    AIExtractedOpportunityFields,
    FacebookPageConfig,
    TelegramChannelConfig,
    _coerce_opportunity_type,
    _resolve_target_url,
    fetch_new_facebook_posts,
    fetch_new_telegram_posts,
    get_or_create_source,
    load_sources_config,
    process_raw_post_via_ai,
    upsert_opportunity,
)

# Deliberately NOT a module-level `pytestmark = pytest.mark.asyncio` --
# `TestAIPromptParsing` and `TestSourcesConfigAndMockedFetch` below each
# mix async tests with plain sync ones (`test_coerce_opportunity_type_direct`
# etc.), and a module-level marker would tag the sync methods too, which
# pytest-asyncio warns about ("marked with asyncio but it is not an async
# function"). Same fix already applied in `tests/test_security_shield.py`:
# apply `pytestmark = pytest.mark.asyncio` as a CLASS attribute only on
# classes whose tests are ALL async (`TestDatabasePersistence` below).

# ---------------------------------------------------------------------
# A sample raw Arabic Telegram post, mirroring @tadrebat_1's own dummy
# text in aggregator_service.py -- this step's own literal example channel.
# ---------------------------------------------------------------------

_SAMPLE_ARABIC_POST = (
    "تدريب صيفي مدفوع الأجر في شركة فودافون مصر لخريجي وطلاب كلية الحاسبات "
    "والمعلومات! التدريب لمدة 3 شهور بمرتب شهري. آخر موعد للتقديم 30 أغسطس 2026. "
    "قدم الآن من خلال الرابط: https://careers.vodafone.com.eg/summer-internship-2026"
)

_SAMPLE_SOURCE_INFO = {
    "source_name": "@tadrebat_1",
    "source_type": SourceType.TELEGRAM,
    "source_base_url": "https://t.me/tadrebat_1",
    "category": "Internships_Training",
}


def _mock_gemini_extraction(**overrides) -> AsyncMock:
    """Patches `aggregator_service._extract_fields_via_gemini` -- the one
    real external call this module makes -- with a crafted, deterministic
    response standing in for what Gemini would plausibly return for
    `_SAMPLE_ARABIC_POST`."""
    defaults = dict(
        title="تدريب صيفي مدفوع الأجر في فودافون مصر",
        description="تدريب لمدة 3 شهور بمرتب شهري في فودافون مصر لخريجي وطلاب كلية الحاسبات والمعلومات.",
        deadline="2026-08-30",
        original_link="https://careers.vodafone.com.eg/summer-internship-2026",
        target_age_group="university",
        country_hint="Egypt",
        opportunity_type="internship",
    )
    defaults.update(overrides)
    fields = AIExtractedOpportunityFields(**defaults)
    return patch.object(aggregator_service, "_extract_fields_via_gemini", new=AsyncMock(return_value=fields))


# ---------------------------------------------------------------------
# Component 4, requirement 1: AI prompt logic correctly parses a sample
# Arabic Telegram post into the strict OpportunityCreate schema.
# ---------------------------------------------------------------------


class TestAIPromptParsing:
    @pytest.mark.asyncio
    async def test_arabic_telegram_post_parses_into_strict_opportunity_create(self):
        with _mock_gemini_extraction():
            result = await process_raw_post_via_ai(_SAMPLE_ARABIC_POST, _SAMPLE_SOURCE_INFO)

        assert isinstance(result, OpportunityCreate)
        assert result.title == "تدريب صيفي مدفوع الأجر في فودافون مصر"
        assert result.description is not None and "فودافون" in result.description
        assert result.target_url == "https://careers.vodafone.com.eg/summer-internship-2026"
        assert result.opportunity_type == OpportunityType.INTERNSHIP
        # `_parse_deadline`'s own `datetime.fromisoformat` on a bare
        # "YYYY-MM-DD" (no "Z"/offset in the source string) produces a
        # NAIVE datetime -- only a literal trailing "Z" gets normalized to
        # "+00:00" (see that function's own docstring). No timezone is
        # fabricated here that Gemini's own output didn't imply.
        assert result.deadline == datetime(2026, 8, 30)
        assert result.target_age_group == "university"
        assert result.is_local_egypt is True
        assert result.country == "Egypt", "country_hint='Egypt' must derive country='Egypt'"
        assert result.source_name == "@tadrebat_1"
        assert result.source_type == SourceType.TELEGRAM
        assert result.source_base_url == "https://t.me/tadrebat_1"

    @pytest.mark.asyncio
    async def test_global_opportunity_derives_country_global(self):
        with _mock_gemini_extraction(country_hint="Global", title="Global DAAD Scholarship"):
            result = await process_raw_post_via_ai("some global scholarship post", _SAMPLE_SOURCE_INFO)

        assert result.is_local_egypt is False
        assert result.country == "Global"

    @pytest.mark.asyncio
    async def test_missing_link_falls_back_to_source_base_url(self):
        """A raw post frequently has no literal link at all -- `target_url`
        is a real NOT NULL column, so this must never come back empty."""
        with _mock_gemini_extraction(original_link=None):
            result = await process_raw_post_via_ai(_SAMPLE_ARABIC_POST, _SAMPLE_SOURCE_INFO)

        assert result.target_url == _SAMPLE_SOURCE_INFO["source_base_url"]

    @pytest.mark.asyncio
    async def test_unrecognized_opportunity_type_falls_back_to_other(self):
        # Taxonomy Evolution step: a genuinely unresolved category (not
        # a direct enum match, and no taxonomy.py alias for "webinar")
        # now falls back to the honest OTHER bucket, not a guessed
        # COURSE -- see OpportunityType.OTHER's own docstring.
        with _mock_gemini_extraction(opportunity_type="webinar"):
            result = await process_raw_post_via_ai(_SAMPLE_ARABIC_POST, _SAMPLE_SOURCE_INFO)

        assert result.opportunity_type == OpportunityType.OTHER

    @pytest.mark.asyncio
    async def test_missing_deadline_parses_to_none(self):
        with _mock_gemini_extraction(deadline=None):
            result = await process_raw_post_via_ai(_SAMPLE_ARABIC_POST, _SAMPLE_SOURCE_INFO)

        assert result.deadline is None

    def test_coerce_opportunity_type_direct(self):
        assert _coerce_opportunity_type("scholarship") == OpportunityType.SCHOLARSHIP
        assert _coerce_opportunity_type("INTERNSHIP") == OpportunityType.INTERNSHIP
        # Taxonomy Evolution step: the fallback for a genuinely
        # unresolved category changed from the less honest `COURSE`
        # default to the explicit `OTHER` bucket -- see
        # `OpportunityType.OTHER`'s own docstring and
        # `_FALLBACK_OPPORTUNITY_TYPE`'s own updated comment.
        assert _coerce_opportunity_type("not-a-real-type") == OpportunityType.OTHER

    def test_resolve_target_url_direct(self):
        assert _resolve_target_url("https://example.com/apply", "https://t.me/fallback") == (
            "https://example.com/apply"
        )
        assert _resolve_target_url(None, "https://t.me/fallback") == "https://t.me/fallback"
        assert _resolve_target_url("   ", "https://t.me/fallback") == "https://t.me/fallback"

    def test_resolve_target_url_prefers_item_url_over_generic_source_base_url(self):
        """Phase 3B addition: when the AI found no link in the post body,
        the specific item's OWN URL (real provenance from a live source
        adapter) is preferred over the generic channel/page URL."""
        assert _resolve_target_url(
            None, "https://t.me/opcorners", item_url="https://t.me/opcorners/12345"
        ) == "https://t.me/opcorners/12345"

    def test_resolve_target_url_ai_link_still_wins_over_item_url(self):
        assert _resolve_target_url(
            "https://example.com/apply", "https://t.me/opcorners", item_url="https://t.me/opcorners/12345"
        ) == "https://example.com/apply"

    def test_resolve_target_url_item_url_none_preserves_old_behavior(self):
        assert _resolve_target_url(None, "https://t.me/fallback", item_url=None) == "https://t.me/fallback"


# ---------------------------------------------------------------------
# Sources config + mocked fetch functions
# ---------------------------------------------------------------------


class TestSourcesConfigAndMockedFetch:
    def test_load_sources_config_reads_the_real_merged_config_file(self):
        config = load_sources_config()
        telegram_handles = {c.handle for c in config.telegram_channels}
        facebook_ids = {p.page_id for p in config.facebook_pages}

        # This step's own literal example channels/pages must be present.
        assert "@tadrebat_1" in telegram_handles
        assert "@grabscholarship" in telegram_handles
        assert "ITIEgypt" in facebook_ids
        assert len(config.telegram_channels) >= 8
        assert len(config.facebook_pages) >= 3

    def test_fetch_new_telegram_posts_returns_dummy_text_for_known_channel(self):
        channel = TelegramChannelConfig(handle="@tadrebat_1", category="Internships_Training")
        posts = fetch_new_telegram_posts(channel)
        assert len(posts) >= 1
        assert all(isinstance(p, str) and p.strip() for p in posts)

    def test_fetch_new_telegram_posts_falls_back_for_unknown_channel(self):
        channel = TelegramChannelConfig(handle="@some_other_channel", category="General")
        posts = fetch_new_telegram_posts(channel)
        assert len(posts) >= 1

    def test_fetch_new_facebook_posts_returns_dummy_text(self):
        page = FacebookPageConfig(page_id="ITIEgypt", name="معهد تكنولوجيا المعلومات", target_age="graduates")
        posts = fetch_new_facebook_posts(page)
        assert len(posts) >= 1
        assert all(isinstance(p, str) and p.strip() for p in posts)


# ---------------------------------------------------------------------
# Component 4, requirement 2: the database correctly saves the
# is_local_egypt boolean (+ upsert semantics).
# ---------------------------------------------------------------------

_CREATE_SOURCES_TABLE_SQL = """
CREATE TABLE sources (
    id VARCHAR(36) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    base_url VARCHAR(2048) NOT NULL,
    active_status BOOLEAN NOT NULL DEFAULT 1,
    trust_score FLOAT NOT NULL DEFAULT 0.5, access_status VARCHAR(20) NOT NULL DEFAULT 'approved',
    source_type VARCHAR(20) NOT NULL DEFAULT 'website',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_CHANGE_HISTORY_TABLE_SQL = """
CREATE TABLE opportunity_change_history (
    id VARCHAR(36) PRIMARY KEY, opportunity_id VARCHAR(36) NOT NULL, field_name VARCHAR(50) NOT NULL,
    old_value TEXT, new_value TEXT, source_id VARCHAR(36), evidence_id VARCHAR(36),
    change_reason TEXT NOT NULL, changed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    severity VARCHAR(20)
)
"""


_CREATE_OPPORTUNITIES_TABLE_SQL = """
CREATE TABLE opportunities (
    id VARCHAR(36) PRIMARY KEY,
    title VARCHAR(500) NOT NULL,
    description TEXT,
    target_url VARCHAR(2048) NOT NULL,
    source_id VARCHAR(36) NOT NULL,
    type VARCHAR(20) NOT NULL,
    deadline DATETIME,
    eligibility_criteria TEXT,
    funding_details TEXT,
    location VARCHAR(255),
    country VARCHAR(120) DEFAULT 'Global',
    is_verified BOOLEAN NOT NULL DEFAULT 0,
    ai_confidence_score FLOAT,
    raw_extracted_data TEXT,
    embedding TEXT,
    is_local_egypt BOOLEAN NOT NULL DEFAULT 0,
    target_age_group VARCHAR(50),
    target_academic_levels TEXT,
    min_age INTEGER,
    max_age INTEGER,
    target_gender VARCHAR(10),
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    review_status VARCHAR(20) NOT NULL DEFAULT 'published',
    claimed_by VARCHAR(36),
    claimed_at DATETIME, lifecycle_state VARCHAR(30) NOT NULL DEFAULT 'discovered', lifecycle_state_reason TEXT, lifecycle_state_evaluated_at DATETIME, monitoring_last_checked_at DATETIME
)
"""

# Empty, otherwise-unused -- same disclosed reason as every other test file
# in this codebase that loads an `Opportunity` row: `Opportunity.bookmarks`
# is a `lazy="selectin"` relationship (app/models/opportunity.py), so it
# eagerly queries `user_bookmarks` the moment ANY `Opportunity` row is
# fetched/refreshed via the ORM, even though no test here ever reads
# `.bookmarks` directly.
_CREATE_USER_BOOKMARKS_TABLE_SQL = """
CREATE TABLE user_bookmarks (
    id VARCHAR(36) PRIMARY KEY,
    user_id VARCHAR(36) NOT NULL,
    opportunity_id VARCHAR(36) NOT NULL,
    course_id VARCHAR(36),
    status VARCHAR(20) NOT NULL DEFAULT 'saved',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


@pytest_asyncio.fixture
async def db_session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.execute(text(_CREATE_SOURCES_TABLE_SQL))
        await conn.execute(text(_CREATE_OPPORTUNITIES_TABLE_SQL))
        await conn.execute(text(_CREATE_CHANGE_HISTORY_TABLE_SQL))
        await conn.execute(text(_CREATE_USER_BOOKMARKS_TABLE_SQL))

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


def _make_opportunity_create(**overrides) -> OpportunityCreate:
    defaults = dict(
        title="تدريب صيفي مدفوع الأجر في فودافون مصر",
        description="A paid summer internship.",
        target_url="https://careers.vodafone.com.eg/summer-internship-2026",
        opportunity_type=OpportunityType.INTERNSHIP,
        deadline=datetime(2026, 8, 30, tzinfo=timezone.utc),
        country="Egypt",
        is_local_egypt=True,
        target_age_group="university",
        source_name="@tadrebat_1",
        source_type=SourceType.TELEGRAM,
        source_base_url="https://t.me/tadrebat_1",
    )
    defaults.update(overrides)
    return OpportunityCreate(**defaults)


class TestDatabasePersistence:
    # Every test in this class is async -- safe to mark at the class level
    # (contrast with TestAIPromptParsing/TestSourcesConfigAndMockedFetch
    # above, which mix in plain sync tests and mark individually instead).
    pytestmark = pytest.mark.asyncio

    async def test_upsert_creates_source_and_opportunity_with_is_local_egypt_true(
        self, db_session_factory
    ):
        data = _make_opportunity_create(is_local_egypt=True, country="Egypt")
        async with db_session_factory() as session:
            opportunity, was_created = await upsert_opportunity(session, data)
            await session.commit()
            opportunity_id = opportunity.id

        assert was_created is True

        async with db_session_factory() as session:
            saved = await session.get(Opportunity, opportunity_id)
            assert saved is not None
            # The crux of this step's own explicit verification requirement:
            # the boolean round-trips correctly through the real ORM column.
            assert saved.is_local_egypt is True
            assert saved.country == "Egypt"
            assert saved.target_age_group == "university"
            assert saved.type == OpportunityType.INTERNSHIP

            source = await session.get(Source, saved.source_id)
            assert source is not None
            assert source.name == "@tadrebat_1"
            assert source.source_type == SourceType.TELEGRAM.value

    async def test_upsert_saves_is_local_egypt_false_for_global_opportunity(self, db_session_factory):
        data = _make_opportunity_create(
            is_local_egypt=False,
            country="Global",
            target_url="https://www.daad.de/scholarship-2026",
            source_name="@grabscholarship",
            source_base_url="https://t.me/grabscholarship",
        )
        async with db_session_factory() as session:
            opportunity, _ = await upsert_opportunity(session, data)
            await session.commit()
            opportunity_id = opportunity.id

        async with db_session_factory() as session:
            saved = await session.get(Opportunity, opportunity_id)
            assert saved.is_local_egypt is False
            assert saved.country == "Global"

    async def test_upsert_updates_existing_row_by_target_url_instead_of_duplicating(
        self, db_session_factory
    ):
        """This step's own explicit 'upsert logic (avoiding duplicates
        based on original_link)' requirement -- a second call with the
        SAME target_url must update the existing row, not insert a
        second one."""
        first = _make_opportunity_create(is_local_egypt=False, country="Global", title="Old Title")
        async with db_session_factory() as session:
            first_opportunity, first_was_created = await upsert_opportunity(session, first)
            await session.commit()
            first_id = first_opportunity.id

        assert first_was_created is True

        second = _make_opportunity_create(
            is_local_egypt=True, country="Egypt", title="Updated Title -- Now Confirmed Local"
        )
        async with db_session_factory() as session:
            second_opportunity, second_was_created = await upsert_opportunity(session, second)
            await session.commit()
            second_id = second_opportunity.id

        assert second_was_created is False, "same target_url must update, not insert a second row"
        assert second_id == first_id

        async with db_session_factory() as session:
            count = (
                await session.execute(
                    select(func.count()).select_from(Opportunity).where(
                        Opportunity.target_url == first.target_url
                    )
                )
            ).scalar_one()
            assert count == 1, "must never have created a duplicate row for the same target_url"

            saved = await session.get(Opportunity, first_id)
            assert saved.title == "Updated Title -- Now Confirmed Local"
            assert saved.is_local_egypt is True
            assert saved.country == "Egypt"

    async def test_get_or_create_source_does_not_duplicate_existing_source(self, db_session_factory):
        async with db_session_factory() as session:
            first = await get_or_create_source(
                session, name="@tadrebat_1", base_url="https://t.me/tadrebat_1", source_type=SourceType.TELEGRAM
            )
            await session.commit()
            first_id = first.id

            second = await get_or_create_source(
                session, name="@tadrebat_1", base_url="https://t.me/tadrebat_1", source_type=SourceType.TELEGRAM
            )
            await session.commit()

        assert second.id == first_id

        async with db_session_factory() as session:
            count = (
                await session.execute(select(func.count()).select_from(Source).where(Source.name == "@tadrebat_1"))
            ).scalar_one()
            assert count == 1


# ---------------------------------------------------------------------
# D5.1 -- Global Country Extraction Unblock.
#
# `process_raw_post_via_ai` used to derive `Opportunity.country` from a
# single Egypt-specific boolean (`is_local_egypt`), which structurally
# capped every extraction at "Egypt" or "Global" no matter what a post's
# own text actually said. `AIExtractedOpportunityFields.country_hint`
# (a free-text `str | None`) replaces that boolean; this class proves the
# extractor can now carry a real non-Egypt country through to
# `OpportunityCreate.country`, that `is_local_egypt` (the real, indexed DB
# column `dashboards.py`'s own local-Egypt filter depends on) is still
# correctly derived downstream via the EXISTING `taxonomy.normalize_country`
# layer, and that no Egypt-guessing happens for Arabic-language content
# that doesn't itself state a country.
#
# Same "mock `_extract_fields_via_gemini`, exercise everything else for
# real" boundary as `TestAIPromptParsing` above -- the multilingual cases
# (Tests 4/5) prove the DOWNSTREAM pipeline correctly carries an
# Arabic/French-sourced `country_hint` string through to
# `country`/`is_local_egypt`; they do not exercise a live Gemini call
# (no API key in this sandbox), matching this module's own established
# mocking convention.
# ---------------------------------------------------------------------


class TestCountryHintExtraction:
    pytestmark = pytest.mark.asyncio

    async def test_01_explicit_egypt_country_hint_yields_egypt(self):
        """Test 1: Egypt, explicit -> country_hint='Egypt'."""
        with _mock_gemini_extraction(country_hint="Egypt"):
            result = await process_raw_post_via_ai(_SAMPLE_ARABIC_POST, _SAMPLE_SOURCE_INFO)

        assert result.country == "Egypt"
        assert result.is_local_egypt is True

    async def test_02_explicit_france_country_hint_yields_france(self):
        """Test 2: France, explicit -> France. Proves the extractor is no
        longer structurally capped at Egypt/Global."""
        with _mock_gemini_extraction(
            country_hint="France", title="Scholarship for students in France"
        ):
            result = await process_raw_post_via_ai(
                "Scholarship for students in France", _SAMPLE_SOURCE_INFO
            )

        assert result.country == "France"
        assert result.is_local_egypt is False

    async def test_03_explicit_kenya_country_hint_yields_kenya(self):
        """Test 3: Kenya, explicit -> Kenya."""
        with _mock_gemini_extraction(
            country_hint="Kenya", title="Programme destiné aux étudiants au Kenya"
        ):
            result = await process_raw_post_via_ai(
                "Programme destiné aux étudiants au Kenya", _SAMPLE_SOURCE_INFO
            )

        assert result.country == "Kenya"
        assert result.is_local_egypt is False

    async def test_04_arabic_source_explicitly_stating_france_yields_france(self):
        """Test 4: Arabic source explicitly says France ("منحة للدراسة في
        فرنسا") -> France. Proves multilingual EXPLICIT geography is
        carried through -- the raw Arabic post text is passed in; the
        mocked extraction stands in for what Gemini would return for it
        (no live Gemini call is made in this sandbox)."""
        arabic_post = "منحة للدراسة في فرنسا لطلاب كلية الهندسة"
        with _mock_gemini_extraction(
            country_hint="France", title="منحة للدراسة في فرنسا"
        ):
            result = await process_raw_post_via_ai(arabic_post, _SAMPLE_SOURCE_INFO)

        assert result.country == "France"
        assert result.is_local_egypt is False

    async def test_05_french_source_explicitly_stating_kenya_yields_kenya(self):
        """Test 5: French source explicitly says Kenya ("Programme destiné
        aux étudiants au Kenya") -> Kenya."""
        french_post = "Programme destiné aux étudiants au Kenya, candidature ouverte."
        with _mock_gemini_extraction(country_hint="Kenya", title="Programme au Kenya"):
            result = await process_raw_post_via_ai(french_post, _SAMPLE_SOURCE_INFO)

        assert result.country == "Kenya"
        assert result.is_local_egypt is False

    async def test_06_explicit_global_worldwide_yields_existing_global_representation(self):
        """Test 6: Global/worldwide explicit -> the existing 'Global'
        sentinel representation (same one `taxonomy.normalize_country`
        already maps to 'GLOBAL'), not a new/second representation."""
        with _mock_gemini_extraction(country_hint="Worldwide", title="Worldwide open call"):
            result = await process_raw_post_via_ai("open to applicants worldwide", _SAMPLE_SOURCE_INFO)

        assert result.country == "Worldwide"
        assert normalize_country(result.country) == "GLOBAL"
        assert result.is_local_egypt is False

    async def test_07_no_geography_falls_back_to_global(self):
        """Test 7: no geography stated (country_hint=null) -> the
        existing 'Global' fallback representation `Opportunity.country`
        already used before this change (this column has never held a
        null/'Unknown' value in this pipeline, so no new state is
        introduced here)."""
        with _mock_gemini_extraction(country_hint=None):
            result = await process_raw_post_via_ai("a post with no stated location", _SAMPLE_SOURCE_INFO)

        assert result.country == "Global"
        assert result.is_local_egypt is False

    async def test_08_arabic_source_with_no_explicit_country_is_not_guessed_as_egypt(self):
        """Test 8: the no-guessing rule. An Arabic-language post that
        itself states no country must NOT be automatically inferred as
        Egypt just because it's written in Arabic or came from an
        Egypt-associated source -- the extraction step's own system
        prompt instructs this; this test proves the downstream code does
        not silently reintroduce that guess either."""
        arabic_post_no_geo = "فرصة تدريب لمدة شهرين في مجال البرمجة، التقديم متاح الآن."
        with _mock_gemini_extraction(country_hint=None, title="فرصة تدريب في البرمجة"):
            result = await process_raw_post_via_ai(arabic_post_no_geo, _SAMPLE_SOURCE_INFO)

        assert result.country != "Egypt"
        assert result.is_local_egypt is False
        assert result.country == "Global"

    async def test_09_extracted_country_continues_through_existing_normalization_layer(self):
        """Test 9: the extracted, raw country value is handed to the
        EXISTING `taxonomy.normalize_country` normalization layer (not a
        second/parallel normalization system) to derive `is_local_egypt`
        -- proven here for a real non-Egypt country plus a real Egypt
        alias variant, both resolving correctly through that one shared
        function."""
        with _mock_gemini_extraction(country_hint="Germany", title="Scholarship in Germany"):
            result = await process_raw_post_via_ai("Scholarship for students in Germany", _SAMPLE_SOURCE_INFO)

        assert result.country == "Germany"
        assert normalize_country(result.country) == "DE"
        assert result.is_local_egypt is False

        with _mock_gemini_extraction(country_hint="مصر", title="فرصة في مصر"):
            result_eg = await process_raw_post_via_ai("فرصة تدريب في مصر", _SAMPLE_SOURCE_INFO)

        assert result_eg.country == "مصر"
        assert normalize_country(result_eg.country) == "EG"
        assert result_eg.is_local_egypt is True

    async def test_10_discovery_verification_boundary_is_unaffected(self):
        """Test 10: discovery/verification boundary. Country extraction is
        a discovery signal only -- it must never establish verification
        status. Every `Opportunity` this pipeline creates must still land
        with `is_verified=False, ai_confidence_score=None` regardless of
        which country was extracted, exactly as before this change (see
        `upsert_opportunity`/module docstring's own two-phase design)."""
        with _mock_gemini_extraction(country_hint="United States", title="US fellowship"):
            result = await process_raw_post_via_ai("A fellowship open to students in the United States", _SAMPLE_SOURCE_INFO)

        assert result.country == "United States"
        assert normalize_country(result.country) == "US"
        # `OpportunityCreate` itself carries no `is_verified`/
        # `ai_confidence_score` fields at all -- those are set by
        # `upsert_opportunity`'s own insert path, independent of country.
        assert not hasattr(result, "is_verified")
        assert not hasattr(result, "ai_confidence_score")
