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
        is_local_egypt=True,
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
        assert result.country == "Egypt", "is_local_egypt=True must derive country='Egypt'"
        assert result.source_name == "@tadrebat_1"
        assert result.source_type == SourceType.TELEGRAM
        assert result.source_base_url == "https://t.me/tadrebat_1"

    @pytest.mark.asyncio
    async def test_global_opportunity_derives_country_global(self):
        with _mock_gemini_extraction(is_local_egypt=False, title="Global DAAD Scholarship"):
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
    async def test_unrecognized_opportunity_type_falls_back_to_course(self):
        with _mock_gemini_extraction(opportunity_type="webinar"):
            result = await process_raw_post_via_ai(_SAMPLE_ARABIC_POST, _SAMPLE_SOURCE_INFO)

        assert result.opportunity_type == OpportunityType.COURSE

    @pytest.mark.asyncio
    async def test_missing_deadline_parses_to_none(self):
        with _mock_gemini_extraction(deadline=None):
            result = await process_raw_post_via_ai(_SAMPLE_ARABIC_POST, _SAMPLE_SOURCE_INFO)

        assert result.deadline is None

    def test_coerce_opportunity_type_direct(self):
        assert _coerce_opportunity_type("scholarship") == OpportunityType.SCHOLARSHIP
        assert _coerce_opportunity_type("INTERNSHIP") == OpportunityType.INTERNSHIP
        assert _coerce_opportunity_type("not-a-real-type") == OpportunityType.COURSE

    def test_resolve_target_url_direct(self):
        assert _resolve_target_url("https://example.com/apply", "https://t.me/fallback") == (
            "https://example.com/apply"
        )
        assert _resolve_target_url(None, "https://t.me/fallback") == "https://t.me/fallback"
        assert _resolve_target_url("   ", "https://t.me/fallback") == "https://t.me/fallback"


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
    source_type VARCHAR(20) NOT NULL DEFAULT 'website',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
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
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
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
