"""Behavioral tests for the Backfill Real Eligibility Data step:

- `app/services/ai.py`'s `VerifiedOpportunitySchema` extraction fields
  (min_age/max_age/target_gender/target_academic_levels) and their
  "null is the correct default" strictness, as consumed by
  `app/workers/tasks.py::_verify_new_opportunities`.
- `app/workers/tasks.py`'s `_coerce_gender`/`_coerce_academic_levels`
  defensive coercion helpers.
- `scripts/backfill_opportunity_eligibility.py`'s dry-run/apply modes.

Gemini's own call is mocked at the `ai_gateway.verify_opportunity`/
`ai_gateway.verify_and_embed` boundary throughout -- same "mock the one
clearly-external-call function, let everything else run for real"
posture `tests/test_aggregator.py` already established -- with crafted,
deterministic `VerifiedOpportunitySchema` responses standing in for three
representative real listings:
  1. an explicit restriction ("women only, ages 18-25, undergraduate")
  2. no restriction stated at all
  3. an ambiguous listing ("open to students") that must NOT produce a
     guessed restriction

DISCLOSED, NECESSARY SCOPE LIMIT (same one `tests/test_opportunities.py`
already documented for the prior Real Age/Academic-Year/Gender
Opportunity Filtering step, hit again here for the identical reason):
`Opportunity.target_academic_levels` is a Postgres `ARRAY` of a native
enum with no SQLite equivalent. Committing a real (non-empty) value into
it via the ORM against this test suite's in-memory SQLite raises a
`CompileError`. The DB-integration tests below therefore only exercise
scenarios where the mocked extraction's `target_academic_levels` is
`None` (never a populated list) on any code path that actually calls
`session.commit()`; `_coerce_academic_levels`'s own correctness --
including the "map education-stage language onto real enum values" and
"drop unrecognized values instead of crashing" behavior -- is instead
covered by the pure, no-DB `TestCoerceHelpers` class below, which
constructs and asserts on the real `AcademicLevel` list without ever
touching a database. The backfill script's DRY-RUN mode is exempt from
this limitation entirely (see its own module docstring: dry-run never
assigns the ORM attributes at all, so it's exercised in
`TestBackfillDryRun` including the academic-levels field for real).
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.enums import AcademicLevel, Gender
from app.models.opportunity import Opportunity
from app.services.ai import VerifiedOpportunitySchema
from app.workers.tasks import _coerce_academic_levels, _coerce_gender, _verify_new_opportunities

# ---------------------------------------------------------------------
# Pure coercion-helper tests -- no DB, no mocking.
# ---------------------------------------------------------------------


class TestCoerceHelpers:
    def test_coerce_gender_valid_values(self):
        assert _coerce_gender("female", uuid.uuid4()) == Gender.FEMALE
        assert _coerce_gender("MALE", uuid.uuid4()) == Gender.MALE

    def test_coerce_gender_none_or_unrecognized_returns_none(self):
        assert _coerce_gender(None, uuid.uuid4()) is None
        assert _coerce_gender("non-binary", uuid.uuid4()) is None
        assert _coerce_gender("", uuid.uuid4()) is None

    def test_coerce_academic_levels_valid_values(self):
        result = _coerce_academic_levels(["secondary_1", "SECONDARY_2"], uuid.uuid4())
        assert result == [AcademicLevel.SECONDARY_1, AcademicLevel.SECONDARY_2]

    def test_coerce_academic_levels_drops_unrecognized_entries_instead_of_crashing(self):
        result = _coerce_academic_levels(["masters", "not-a-real-level"], uuid.uuid4())
        assert result == [AcademicLevel.MASTERS]

    def test_coerce_academic_levels_all_unrecognized_returns_none_not_empty_list(self):
        result = _coerce_academic_levels(["invalid_1", "invalid_2"], uuid.uuid4())
        assert result is None

    def test_coerce_academic_levels_none_or_empty_returns_none(self):
        assert _coerce_academic_levels(None, uuid.uuid4()) is None
        assert _coerce_academic_levels([], uuid.uuid4()) is None


# ---------------------------------------------------------------------
# DB-integration tests for `_verify_new_opportunities` -- SQLite,
# SQLite-compatible fields only (min_age/max_age/target_gender). See
# module docstring for the academic_level exclusion.
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


_CREATE_CLAIMS_TABLE_SQL = """
CREATE TABLE claims (
    id VARCHAR(36) PRIMARY KEY, opportunity_id VARCHAR(36) NOT NULL, claim_type VARCHAR(50) NOT NULL,
    claim_value TEXT NOT NULL, normalized_value TEXT, status VARCHAR(20) NOT NULL DEFAULT 'unknown',
    importance VARCHAR(20) NOT NULL DEFAULT 'normal',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_EVIDENCE_TABLE_SQL = """
CREATE TABLE evidence (
    id VARCHAR(36) PRIMARY KEY, opportunity_id VARCHAR(36) NOT NULL, claim_id VARCHAR(36) NOT NULL,
    source_id VARCHAR(36), source_url VARCHAR(2048), source_type VARCHAR(50),
    source_authority VARCHAR(20) NOT NULL DEFAULT 'unknown', evidence_text TEXT NOT NULL,
    evidence_hash VARCHAR(64) NOT NULL, retrieved_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    published_at DATETIME, valid_from DATETIME, valid_until DATETIME, supports_claim BOOLEAN,
    extraction_method VARCHAR(50) NOT NULL DEFAULT 'manual', retrieval_method VARCHAR(50) NOT NULL DEFAULT 'automated',
    evidence_metadata TEXT, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_RESEARCH_TRAJECTORIES_TABLE_SQL = """
CREATE TABLE research_trajectories (
    id VARCHAR(36) PRIMARY KEY,
    objective VARCHAR(500) NOT NULL,
    normalized_objective VARCHAR(500) NOT NULL,
    started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at DATETIME,
    status VARCHAR(20) NOT NULL DEFAULT 'running',
    total_steps INTEGER NOT NULL DEFAULT 0,
    successful_steps INTEGER NOT NULL DEFAULT 0,
    failed_steps INTEGER NOT NULL DEFAULT 0,
    opportunities_discovered INTEGER NOT NULL DEFAULT 0,
    opportunities_verified INTEGER NOT NULL DEFAULT 0,
    useful_sources TEXT,
    research_depth INTEGER NOT NULL DEFAULT 0,
    final_outcome VARCHAR(20),
    trajectory_metadata TEXT
)
"""

_CREATE_RESEARCH_TRAJECTORY_STEPS_TABLE_SQL = """
CREATE TABLE research_trajectory_steps (
    id VARCHAR(36) PRIMARY KEY,
    trajectory_id VARCHAR(36) NOT NULL,
    step_number INTEGER NOT NULL,
    action_type VARCHAR(50) NOT NULL,
    query VARCHAR(500),
    source_domain VARCHAR(500),
    reference_id VARCHAR(100),
    parent_step_id VARCHAR(36),
    outcome VARCHAR(20) NOT NULL DEFAULT 'neutral',
    usefulness_score FLOAT,
    discovered_entities_count INTEGER NOT NULL DEFAULT 0,
    discovered_opportunities_count INTEGER NOT NULL DEFAULT 0,
    verification_impact VARCHAR(50),
    error_reason VARCHAR(500),
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    step_metadata TEXT
)
"""

_CREATE_ENTITY_RELATIONSHIPS_TABLE_SQL = """
CREATE TABLE entity_relationships (
    id VARCHAR(36) PRIMARY KEY,
    opportunity_id VARCHAR(36) NOT NULL,
    related_opportunity_id VARCHAR(36) NOT NULL,
    relationship_type VARCHAR(30) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

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
        await conn.execute(text(_CREATE_ENTITY_RELATIONSHIPS_TABLE_SQL))
        await conn.execute(text(_CREATE_RESEARCH_TRAJECTORIES_TABLE_SQL))
        await conn.execute(text(_CREATE_RESEARCH_TRAJECTORY_STEPS_TABLE_SQL))
        await conn.execute(text(_CREATE_CLAIMS_TABLE_SQL))
        await conn.execute(text(_CREATE_EVIDENCE_TABLE_SQL))

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


async def _make_source(db: AsyncSession) -> uuid.UUID:
    source_id = uuid.uuid4()
    await db.execute(
        text("INSERT INTO sources (id, name, base_url) VALUES (:id, 'Test Source', 'https://example.com')"),
        {"id": source_id.hex},
    )
    await db.commit()
    return source_id


async def _make_unverified_opportunity(
    db: AsyncSession, *, source_id: uuid.UUID, raw_extracted_data: str | None, title: str = "Some Opportunity"
) -> uuid.UUID:
    opportunity_id = uuid.uuid4()
    await db.execute(
        text(
            """
            INSERT INTO opportunities
                (id, title, target_url, source_id, type, is_verified, ai_confidence_score, raw_extracted_data)
            VALUES
                (:id, :title, 'https://example.com/apply', :source_id, 'scholarship', 0, NULL, :raw)
            """
        ),
        {"id": opportunity_id.hex, "title": title, "source_id": source_id.hex, "raw": raw_extracted_data},
    )
    await db.commit()
    return opportunity_id


def _mock_verify_and_embed(**overrides):
    defaults = dict(
        title="Cleaned Title",
        deadline=None,
        eligibility_criteria="Some cleaned eligibility text.",
        funding_details=None,
        is_legitimate=True,
        confidence_score=0.9,
        min_age=None,
        max_age=None,
        target_gender=None,
        target_academic_levels=None,
    )
    defaults.update(overrides)
    schema = VerifiedOpportunitySchema(**defaults)
    return patch(
        "app.workers.tasks.ai_gateway.verify_and_embed",
        # 1536 dims to match `Opportunity.embedding`'s real `Vector(1536)`
        # column (app/models/opportunity.py) -- pgvector's own bind
        # processor enforces the exact dimension count even against SQLite.
        new=AsyncMock(return_value=(schema, [0.1] * 1536)),
    )


@pytest.mark.asyncio
class TestVerifyNewOpportunitiesEligibilityExtraction:
    async def test_explicit_restriction_is_extracted_and_persisted(self, db_session_factory):
        """Representative sample #1: 'Women in STEM Scholarship -- open to
        female undergraduate students aged 18-25.' An explicit restriction
        on age and gender must be extracted and written for real."""
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            opportunity_id = await _make_unverified_opportunity(
                db,
                source_id=source_id,
                title="Women in STEM Scholarship",
                raw_extracted_data='{"description": "Open to female undergraduate students aged 18-25."}',
            )

        with _mock_verify_and_embed(min_age=18, max_age=25, target_gender="female"):
            with patch("app.workers.tasks.AsyncSessionLocal", db_session_factory), patch(
                "app.workers.tasks.engine", new=AsyncMock()
            ):
                summary = await _verify_new_opportunities(limit=10)

        assert summary == {"processed": 1, "verified": 1, "flagged": 0, "errors": 0}

        async with db_session_factory() as db:
            saved = await db.get(Opportunity, opportunity_id)
            assert saved.min_age == 18
            assert saved.max_age == 25
            assert saved.target_gender == Gender.FEMALE
            assert saved.is_verified is True

    async def test_no_restriction_stated_leaves_fields_null(self, db_session_factory):
        """Representative sample #2: a listing with no eligibility
        restriction mentioned at all -- every new field must stay NULL
        (unrestricted), not default to some guessed value."""
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            opportunity_id = await _make_unverified_opportunity(
                db,
                source_id=source_id,
                title="Open To Everyone Internship",
                raw_extracted_data='{"description": "A paid internship. Apply via the link."}',
            )

        with _mock_verify_and_embed():  # every eligibility field defaults to None
            with patch("app.workers.tasks.AsyncSessionLocal", db_session_factory), patch(
                "app.workers.tasks.engine", new=AsyncMock()
            ):
                summary = await _verify_new_opportunities(limit=10)

        assert summary["verified"] == 1

        async with db_session_factory() as db:
            saved = await db.get(Opportunity, opportunity_id)
            assert saved.min_age is None
            assert saved.max_age is None
            assert saved.target_gender is None
            assert saved.target_academic_levels is None

    async def test_ambiguous_listing_does_not_invent_a_restriction(self, db_session_factory):
        """Representative sample #3: 'Open to students who want to grow
        their skills' -- mentions 'students' but names no identifiable
        age, gender, or academic stage. Per the prompt's own strictness
        rule, this must NOT be coerced into a guessed restriction --
        this test asserts the CORRECT behavior is leaving every field
        null, exactly like the mocked response below (standing in for
        what the real model is instructed to return for this input)."""
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            opportunity_id = await _make_unverified_opportunity(
                db,
                source_id=source_id,
                title="Skills Growth Program",
                raw_extracted_data='{"description": "Open to students who want to grow their skills."}',
            )

        # Standing in for a correctly-strict real Gemini response: the
        # word "students" alone must not become an age/gender/level guess.
        with _mock_verify_and_embed(min_age=None, max_age=None, target_gender=None, target_academic_levels=None):
            with patch("app.workers.tasks.AsyncSessionLocal", db_session_factory), patch(
                "app.workers.tasks.engine", new=AsyncMock()
            ):
                summary = await _verify_new_opportunities(limit=10)

        assert summary["verified"] == 1

        async with db_session_factory() as db:
            saved = await db.get(Opportunity, opportunity_id)
            assert saved.min_age is None
            assert saved.max_age is None
            assert saved.target_gender is None
            assert saved.target_academic_levels is None

    async def test_unrecognized_gender_value_is_dropped_not_crashed_on(self, db_session_factory):
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            opportunity_id = await _make_unverified_opportunity(
                db, source_id=source_id, raw_extracted_data='{"description": "..."}'
            )

        with _mock_verify_and_embed(target_gender="everyone"):
            with patch("app.workers.tasks.AsyncSessionLocal", db_session_factory), patch(
                "app.workers.tasks.engine", new=AsyncMock()
            ):
                summary = await _verify_new_opportunities(limit=10)

        assert summary["errors"] == 0
        async with db_session_factory() as db:
            saved = await db.get(Opportunity, opportunity_id)
            assert saved.target_gender is None


# ---------------------------------------------------------------------
# Backfill script tests.
# ---------------------------------------------------------------------


@pytest.mark.asyncio
class TestBackfillDryRun:
    async def test_dry_run_reports_would_be_changes_without_writing(self, db_session_factory, monkeypatch):
        from scripts import backfill_opportunity_eligibility as backfill

        async with db_session_factory() as db:
            source_id = await _make_source(db)
            opportunity_id = await _make_unverified_opportunity(
                db,
                source_id=source_id,
                title="Women in STEM Scholarship",
                raw_extracted_data='{"description": "Open to female undergraduate students aged 18-25."}',
            )

        monkeypatch.setattr(backfill, "AsyncSessionLocal", db_session_factory)
        with _mock_verify_and_embed_for_backfill(
            min_age=18, max_age=25, target_gender="female", target_academic_levels=["university_year_2"]
        ):
            summary = await backfill._run(apply=False, limit=None)

        assert summary["mode"].startswith("DRY RUN")
        assert summary["candidates_processed"] == 1
        assert summary["rows_with_a_new_restriction_found"] == 1
        assert summary["sample_changes"][0]["target_gender"] == "female"
        # Dry run: real DB row must be completely untouched.
        async with db_session_factory() as db:
            saved = await db.get(Opportunity, opportunity_id)
            assert saved.min_age is None
            assert saved.max_age is None
            assert saved.target_gender is None

    async def test_already_backfilled_and_no_raw_data_rows_are_counted_separately(
        self, db_session_factory, monkeypatch
    ):
        from scripts import backfill_opportunity_eligibility as backfill

        async with db_session_factory() as db:
            source_id = await _make_source(db)
            # No raw_extracted_data at all -- unbackfillable.
            await _make_unverified_opportunity(db, source_id=source_id, raw_extracted_data=None)
            # Real candidate.
            await _make_unverified_opportunity(
                db, source_id=source_id, raw_extracted_data='{"description": "..."}'
            )
            # Already has a real min_age -- must be skipped, not re-guessed.
            already_id = uuid.uuid4()
            await db.execute(
                text(
                    """
                    INSERT INTO opportunities
                        (id, title, target_url, source_id, type, is_verified, raw_extracted_data, min_age)
                    VALUES
                        (:id, 'Already Backfilled', 'https://example.com/x', :source_id, 'scholarship', 1, '{}', 18)
                    """
                ),
                {"id": already_id.hex, "source_id": source_id.hex},
            )
            await db.commit()

        monkeypatch.setattr(backfill, "AsyncSessionLocal", db_session_factory)
        with _mock_verify_and_embed_for_backfill():
            summary = await backfill._run(apply=False, limit=None)

        assert summary["no_raw_extracted_data_unbackfillable"] == 1
        assert summary["already_backfilled_skipped"] == 1
        assert summary["candidates_processed"] == 1


@pytest.mark.asyncio
class TestBackfillApply:
    async def test_apply_writes_sqlite_compatible_fields_for_real(self, db_session_factory, monkeypatch):
        """Only min_age/max_age/target_gender here -- see module docstring
        for why target_academic_levels is excluded from any real-commit
        (`apply=True`) test."""
        from scripts import backfill_opportunity_eligibility as backfill

        async with db_session_factory() as db:
            source_id = await _make_source(db)
            opportunity_id = await _make_unverified_opportunity(
                db,
                source_id=source_id,
                title="Adults Only Program",
                raw_extracted_data='{"description": "Must be 18 or older."}',
            )

        monkeypatch.setattr(backfill, "AsyncSessionLocal", db_session_factory)
        with _mock_verify_and_embed_for_backfill(min_age=18):
            summary = await backfill._run(apply=True, limit=None)

        assert summary["mode"].startswith("APPLY")
        assert summary["rows_with_a_new_restriction_found"] == 1

        async with db_session_factory() as db:
            saved = await db.get(Opportunity, opportunity_id)
            assert saved.min_age == 18

    async def test_apply_with_no_restriction_found_writes_nothing(self, db_session_factory, monkeypatch):
        from scripts import backfill_opportunity_eligibility as backfill

        async with db_session_factory() as db:
            source_id = await _make_source(db)
            opportunity_id = await _make_unverified_opportunity(
                db, source_id=source_id, raw_extracted_data='{"description": "Open to everyone."}'
            )

        monkeypatch.setattr(backfill, "AsyncSessionLocal", db_session_factory)
        with _mock_verify_and_embed_for_backfill():
            summary = await backfill._run(apply=True, limit=None)

        assert summary["rows_with_a_new_restriction_found"] == 0
        async with db_session_factory() as db:
            saved = await db.get(Opportunity, opportunity_id)
            assert saved.min_age is None
            assert saved.target_gender is None


def _mock_verify_and_embed_for_backfill(**overrides):
    """Same crafted-response helper as `_mock_verify_and_embed` above, but
    patches `ai_gateway.verify_opportunity` directly (what the backfill
    script itself calls -- it doesn't need the embedding half of
    `verify_and_embed`, since it never touches `Opportunity.embedding`)."""
    defaults = dict(
        title="Cleaned Title",
        deadline=None,
        eligibility_criteria="Some cleaned eligibility text.",
        funding_details=None,
        is_legitimate=True,
        confidence_score=0.9,
        min_age=None,
        max_age=None,
        target_gender=None,
        target_academic_levels=None,
    )
    defaults.update(overrides)
    schema = VerifiedOpportunitySchema(**defaults)
    return patch(
        "scripts.backfill_opportunity_eligibility.ai_gateway.verify_opportunity",
        new=AsyncMock(return_value=schema),
    )
