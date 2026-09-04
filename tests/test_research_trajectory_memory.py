"""Behavioral tests for Search Trajectory Memory (Deliverable 3):

- ResearchTrajectory/ResearchTrajectoryStep creation, recording,
  completion, branching (parent_step_id), persistence across a fresh
  session (simulating "across sessions" -- a real commit + a real
  re-query in a NEW AsyncSession, not just re-reading the same
  in-memory object).
- retrieve_similar_trajectories (deterministic keyword-overlap, no
  vector DB), retrieve_successful_search_patterns,
  retrieve_failed_search_patterns, get_source_performance,
  build_research_hints -- including the real "no memory" case.
- apply_verification_outcome closing the loop from a real, decoupled
  verification pass back onto the real step that discovered an
  opportunity -- and the explicit assertion that trajectory memory
  hints NEVER override or feed back into `Opportunity.is_verified`/
  `ai_confidence_score` (Evidence + Verification stays authoritative).
- Real pipeline integration: `_run_daily_opportunity_scraper`
  (app/workers/aggregator_tasks.py) creates a real, completed
  trajectory with real steps, with `process_raw_post_via_ai` mocked at
  the same Gemini boundary `tests/test_aggregator.py` already
  establishes as this codebase's convention.
- Real HTTP layer for the three new admin endpoints (tier gating, 404,
  hints, source-performance).

Real SQLite (in-memory), same established technique as every other
test file in this project.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user
from app.db.session import get_db
from app.main import app
from app.models.enums import AdminRole, TrajectoryOutcome, TrajectoryStatus
from app.models.opportunity import Opportunity
from app.models.research_trajectory import ResearchTrajectory, ResearchTrajectoryStep
from app.models.user import User
from app.services import research_trajectory_memory

# ---------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------

_CREATE_RESEARCH_TRAJECTORIES_SQL = """
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

_CREATE_RESEARCH_TRAJECTORY_STEPS_SQL = """
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

_CREATE_USERS_TABLE_SQL = """
CREATE TABLE users (
    id VARCHAR(36) PRIMARY KEY, email VARCHAR(255) UNIQUE, hashed_password VARCHAR(255),
    full_name VARCHAR(255), is_active BOOLEAN NOT NULL DEFAULT 1, is_superuser BOOLEAN NOT NULL DEFAULT 0,
    phone VARCHAR(50), degree_level VARCHAR(100), major VARCHAR(255), gpa FLOAT, graduation_year INTEGER,
    skills TEXT, target_country VARCHAR(120), age_group VARCHAR(50), age INTEGER, academic_level VARCHAR(20),
    gender VARCHAR(10), bio TEXT, search_vector TEXT, role VARCHAR(20) NOT NULL DEFAULT 'student',
    admin_role VARCHAR(20), role_granted_at DATETIME, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_USER_BOOKMARKS_TABLE_SQL = """
CREATE TABLE user_bookmarks (
    id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36) NOT NULL, opportunity_id VARCHAR(36) NOT NULL,
    course_id VARCHAR(36), status VARCHAR(20) NOT NULL DEFAULT 'saved',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_SOURCES_TABLE_SQL = """
CREATE TABLE sources (
    id VARCHAR(36) PRIMARY KEY, name VARCHAR(255) NOT NULL, base_url VARCHAR(2048) NOT NULL,
    active_status BOOLEAN NOT NULL DEFAULT 1, source_type VARCHAR(20) NOT NULL DEFAULT 'website',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_ENTITY_RELATIONSHIPS_TABLE_SQL = """
CREATE TABLE entity_relationships (
    id VARCHAR(36) PRIMARY KEY, opportunity_id VARCHAR(36) NOT NULL, related_opportunity_id VARCHAR(36) NOT NULL,
    relationship_type VARCHAR(30) NOT NULL, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_OPPORTUNITIES_TABLE_SQL = """
CREATE TABLE opportunities (
    id VARCHAR(36) PRIMARY KEY, title VARCHAR(500) NOT NULL, description TEXT,
    target_url VARCHAR(2048) NOT NULL, source_id VARCHAR(36) NOT NULL, type VARCHAR(20) NOT NULL,
    deadline DATETIME, eligibility_criteria TEXT, funding_details TEXT, location VARCHAR(255),
    country VARCHAR(120) DEFAULT 'Global', is_verified BOOLEAN NOT NULL DEFAULT 0, ai_confidence_score FLOAT,
    raw_extracted_data TEXT, embedding TEXT, is_local_egypt BOOLEAN NOT NULL DEFAULT 0,
    target_age_group VARCHAR(50), target_academic_levels TEXT, min_age INTEGER, max_age INTEGER,
    target_gender VARCHAR(10), review_status VARCHAR(20) NOT NULL DEFAULT 'published',
    claimed_by VARCHAR(36), claimed_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.execute(text(_CREATE_USERS_TABLE_SQL))
        await conn.execute(text(_CREATE_USER_BOOKMARKS_TABLE_SQL))
        await conn.execute(text(_CREATE_SOURCES_TABLE_SQL))
        await conn.execute(text(_CREATE_OPPORTUNITIES_TABLE_SQL))
        await conn.execute(text(_CREATE_ENTITY_RELATIONSHIPS_TABLE_SQL))
        await conn.execute(text(_CREATE_RESEARCH_TRAJECTORIES_SQL))
        await conn.execute(text(_CREATE_RESEARCH_TRAJECTORY_STEPS_SQL))
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session_factory(db_engine):
    return async_sessionmaker(bind=db_engine, class_=AsyncSession, expire_on_commit=False)


async def _make_source_and_opportunity(db: AsyncSession, *, opp_id: uuid.UUID | None = None) -> uuid.UUID:
    source_id = uuid.uuid4()
    await db.execute(
        text("INSERT INTO sources (id, name, base_url) VALUES (:id, 'Test Source', 'https://example.com')"),
        {"id": source_id.hex},
    )
    opp_id = opp_id or uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO opportunities (id, title, target_url, source_id, type) "
            "VALUES (:id, 'Some Opportunity', 'https://example.com/apply', :source_id, 'scholarship')"
        ),
        {"id": opp_id.hex, "source_id": source_id.hex},
    )
    await db.commit()
    return opp_id


# ---------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------


class TestTrajectoryCreation:
    @pytest.mark.asyncio
    async def test_start_trajectory_creates_a_real_running_row(self, db_session_factory):
        async with db_session_factory() as db:
            trajectory = await research_trajectory_memory.start_trajectory(
                db, objective="Find scholarships for Egyptian CS undergrads"
            )
            await db.commit()

            assert trajectory.id is not None
            assert trajectory.status == TrajectoryStatus.RUNNING.value
            assert trajectory.normalized_objective == "find scholarships for egyptian cs undergrads"
            assert trajectory.total_steps == 0
            assert trajectory.completed_at is None

    @pytest.mark.asyncio
    async def test_normalize_objective_is_deterministic(self):
        a = research_trajectory_memory.normalize_objective("  Find   Scholarships!! (Egypt)  ")
        b = research_trajectory_memory.normalize_objective("find scholarships egypt")
        assert a == "find scholarships egypt"
        assert a == b  # same real underlying text, correctly normalizes identically
        c = research_trajectory_memory.normalize_objective("Find internships in Jordan")
        assert a != c  # genuinely different real text, correctly NOT equal


class TestStepRecording:
    @pytest.mark.asyncio
    async def test_record_step_assigns_sequential_step_numbers(self, db_session_factory):
        async with db_session_factory() as db:
            trajectory = await research_trajectory_memory.start_trajectory(db, objective="Sweep sources")
            step1 = await research_trajectory_memory.record_step(
                db, trajectory=trajectory, action_type="query_source", query="@channel1"
            )
            step2 = await research_trajectory_memory.record_step(
                db, trajectory=trajectory, action_type="query_source", query="@channel2"
            )
            await db.commit()

            assert step1.step_number == 1
            assert step2.step_number == 2
            assert trajectory.total_steps == 2

    @pytest.mark.asyncio
    async def test_branching_via_parent_step_id(self, db_session_factory):
        async with db_session_factory() as db:
            trajectory = await research_trajectory_memory.start_trajectory(db, objective="Sweep sources")
            parent = await research_trajectory_memory.record_step(
                db, trajectory=trajectory, action_type="query_source", query="@channel1",
                source_domain="https://t.me/channel1",
            )
            child = await research_trajectory_memory.record_step(
                db, trajectory=trajectory, action_type="process_result", query="@channel1",
                source_domain="https://t.me/channel1", parent_step_id=parent.id,
            )
            await db.commit()

            assert child.parent_step_id == parent.id

    @pytest.mark.asyncio
    async def test_complete_step_updates_trajectory_rollups(self, db_session_factory):
        async with db_session_factory() as db:
            trajectory = await research_trajectory_memory.start_trajectory(db, objective="Sweep")
            step = await research_trajectory_memory.record_step(
                db, trajectory=trajectory, action_type="process_result", query="q"
            )
            await research_trajectory_memory.complete_step(
                db, step=step, trajectory=trajectory,
                outcome=TrajectoryOutcome.SUCCESSFUL, usefulness_score=0.9,
                discovered_opportunities_count=1, discovered_entities_count=1,
            )
            await db.commit()

            assert trajectory.successful_steps == 1
            assert trajectory.failed_steps == 0
            assert trajectory.opportunities_discovered == 1
            assert step.outcome == TrajectoryOutcome.SUCCESSFUL.value
            assert step.usefulness_score == 0.9


class TestTrajectoryOutcomes:
    @pytest.mark.asyncio
    async def test_successful_trajectory(self, db_session_factory):
        async with db_session_factory() as db:
            trajectory = await research_trajectory_memory.start_trajectory(db, objective="Sweep A")
            step = await research_trajectory_memory.record_step(
                db, trajectory=trajectory, action_type="process_result", query="q1", source_domain="a.example.com"
            )
            await research_trajectory_memory.complete_step(
                db, step=step, trajectory=trajectory, outcome=TrajectoryOutcome.SUCCESSFUL,
                discovered_opportunities_count=1,
            )
            trajectory.opportunities_verified = 1  # simulate the real verification loop having closed
            completed = await research_trajectory_memory.record_outcome(db, trajectory=trajectory)
            await db.commit()

            assert completed.status == TrajectoryStatus.COMPLETED.value
            assert completed.final_outcome == TrajectoryOutcome.SUCCESSFUL.value
            assert completed.completed_at is not None
            assert completed.useful_sources == ["a.example.com"]
            assert completed.research_depth == 1

    @pytest.mark.asyncio
    async def test_failed_trajectory(self, db_session_factory):
        async with db_session_factory() as db:
            trajectory = await research_trajectory_memory.start_trajectory(db, objective="Sweep B")
            step = await research_trajectory_memory.record_step(
                db, trajectory=trajectory, action_type="process_result", query="q1"
            )
            await research_trajectory_memory.complete_step(
                db, step=step, trajectory=trajectory, outcome=TrajectoryOutcome.FAILED,
                error_reason="AI extraction failed",
            )
            completed = await research_trajectory_memory.record_outcome(db, trajectory=trajectory)
            await db.commit()

            assert completed.status == TrajectoryStatus.FAILED.value
            assert completed.final_outcome == TrajectoryOutcome.FAILED.value
            assert completed.useful_sources == []

    @pytest.mark.asyncio
    async def test_partially_useful_trajectory(self, db_session_factory):
        async with db_session_factory() as db:
            trajectory = await research_trajectory_memory.start_trajectory(db, objective="Sweep C")
            good_step = await research_trajectory_memory.record_step(
                db, trajectory=trajectory, action_type="process_result", query="q1"
            )
            bad_step = await research_trajectory_memory.record_step(
                db, trajectory=trajectory, action_type="process_result", query="q2"
            )
            await research_trajectory_memory.complete_step(
                db, step=good_step, trajectory=trajectory, outcome=TrajectoryOutcome.SUCCESSFUL,
                discovered_opportunities_count=1,
            )
            await research_trajectory_memory.complete_step(
                db, step=bad_step, trajectory=trajectory, outcome=TrajectoryOutcome.FAILED,
                error_reason="nothing found",
            )
            completed = await research_trajectory_memory.record_outcome(db, trajectory=trajectory)
            await db.commit()

            assert completed.final_outcome == TrajectoryOutcome.PARTIALLY_USEFUL.value
            assert completed.status == TrajectoryStatus.COMPLETED.value

    @pytest.mark.asyncio
    async def test_neutral_trajectory_with_no_steps(self, db_session_factory):
        async with db_session_factory() as db:
            trajectory = await research_trajectory_memory.start_trajectory(db, objective="Empty sweep")
            completed = await research_trajectory_memory.record_outcome(db, trajectory=trajectory)
            await db.commit()

            assert completed.final_outcome == TrajectoryOutcome.NEUTRAL.value
            assert completed.research_depth == 0


class TestVerificationLoop:
    @pytest.mark.asyncio
    async def test_apply_verification_outcome_updates_the_originating_step(self, db_session_factory):
        async with db_session_factory() as db:
            opp_id = await _make_source_and_opportunity(db)
            trajectory = await research_trajectory_memory.start_trajectory(db, objective="Sweep D")
            step = await research_trajectory_memory.record_step(
                db, trajectory=trajectory, action_type="process_result", query="q1", source_domain="a.example.com"
            )
            step.reference_id = str(opp_id)
            await research_trajectory_memory.complete_step(
                db, step=step, trajectory=trajectory, outcome=TrajectoryOutcome.NEUTRAL,
                discovered_opportunities_count=1,
            )
            await db.commit()

            updated_step = await research_trajectory_memory.apply_verification_outcome(
                db, opportunity_id=opp_id, is_legitimate=True, confidence_score=0.95
            )
            await db.commit()

            assert updated_step is not None
            assert updated_step.outcome == TrajectoryOutcome.SUCCESSFUL.value
            assert updated_step.verification_impact == "verified_legitimate"
            assert updated_step.usefulness_score == 0.95

            refreshed_trajectory = await db.get(ResearchTrajectory, trajectory.id)
            assert refreshed_trajectory.opportunities_verified == 1

    @pytest.mark.asyncio
    async def test_apply_verification_outcome_marks_misleading_when_not_legitimate(self, db_session_factory):
        async with db_session_factory() as db:
            opp_id = await _make_source_and_opportunity(db)
            trajectory = await research_trajectory_memory.start_trajectory(db, objective="Sweep E")
            step = await research_trajectory_memory.record_step(
                db, trajectory=trajectory, action_type="process_result", query="q1"
            )
            step.reference_id = str(opp_id)
            await research_trajectory_memory.complete_step(
                db, step=step, trajectory=trajectory, outcome=TrajectoryOutcome.NEUTRAL,
            )
            await db.commit()

            updated_step = await research_trajectory_memory.apply_verification_outcome(
                db, opportunity_id=opp_id, is_legitimate=False, confidence_score=0.2
            )
            await db.commit()

            assert updated_step.outcome == TrajectoryOutcome.MISLEADING.value
            assert updated_step.verification_impact == "verified_not_legitimate"

    @pytest.mark.asyncio
    async def test_apply_verification_outcome_is_a_real_noop_for_unmatched_opportunity(self, db_session_factory):
        """An opportunity the pre-existing Scrapy pipeline created (or any
        opportunity with no matching trajectory step) must not crash or
        fabricate a step."""
        async with db_session_factory() as db:
            unmatched_id = uuid.uuid4()
            result = await research_trajectory_memory.apply_verification_outcome(
                db, opportunity_id=unmatched_id, is_legitimate=True, confidence_score=0.9
            )
            assert result is None

    @pytest.mark.asyncio
    async def test_verification_outcome_never_writes_to_opportunity(self, db_session_factory):
        """THE critical boundary test for this deliverable: trajectory
        memory must NEVER become a second source of truth for
        `Opportunity.is_verified`/`ai_confidence_score` -- verification
        stays exclusively owned by ai_gateway/_verify_new_opportunities.
        """
        async with db_session_factory() as db:
            opp_id = await _make_source_and_opportunity(db)
            trajectory = await research_trajectory_memory.start_trajectory(db, objective="Sweep F")
            step = await research_trajectory_memory.record_step(
                db, trajectory=trajectory, action_type="process_result", query="q1"
            )
            step.reference_id = str(opp_id)
            await db.commit()

            before = await db.execute(text("SELECT is_verified, ai_confidence_score FROM opportunities WHERE id = :id"), {"id": opp_id.hex})
            before_row = before.first()

            await research_trajectory_memory.apply_verification_outcome(
                db, opportunity_id=opp_id, is_legitimate=True, confidence_score=0.99
            )
            await db.commit()

            after = await db.execute(text("SELECT is_verified, ai_confidence_score FROM opportunities WHERE id = :id"), {"id": opp_id.hex})
            after_row = after.first()

            assert before_row == after_row, "apply_verification_outcome must never mutate Opportunity"


# ---------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------


class TestRetrieval:
    @pytest.mark.asyncio
    async def test_retrieve_similar_trajectories_ranks_by_keyword_overlap(self, db_session_factory):
        async with db_session_factory() as db:
            t1 = await research_trajectory_memory.start_trajectory(db, objective="Find scholarships for engineering students in Egypt")
            await research_trajectory_memory.record_outcome(db, trajectory=t1, final_outcome=TrajectoryOutcome.SUCCESSFUL)
            t2 = await research_trajectory_memory.start_trajectory(db, objective="Find internships for design students in Jordan")
            await research_trajectory_memory.record_outcome(db, trajectory=t2, final_outcome=TrajectoryOutcome.SUCCESSFUL)
            await db.commit()

            results = await research_trajectory_memory.retrieve_similar_trajectories(
                db, objective="Find scholarships for engineering students"
            )
            assert len(results) >= 1
            assert results[0][0].id == t1.id
            assert results[0][1] > 0

    @pytest.mark.asyncio
    async def test_retrieve_similar_trajectories_excludes_running_trajectories(self, db_session_factory):
        async with db_session_factory() as db:
            await research_trajectory_memory.start_trajectory(db, objective="Find scholarships for engineering students")
            await db.commit()

            results = await research_trajectory_memory.retrieve_similar_trajectories(
                db, objective="Find scholarships for engineering students"
            )
            assert results == []  # still RUNNING, not COMPLETED -- correctly excluded

    @pytest.mark.asyncio
    async def test_successful_and_failed_query_patterns(self, db_session_factory):
        async with db_session_factory() as db:
            trajectory = await research_trajectory_memory.start_trajectory(db, objective="Sweep")
            good = await research_trajectory_memory.record_step(
                db, trajectory=trajectory, action_type="process_result", query="daad scholarship"
            )
            await research_trajectory_memory.complete_step(
                db, step=good, trajectory=trajectory, outcome=TrajectoryOutcome.SUCCESSFUL, usefulness_score=0.9,
            )
            bad = await research_trajectory_memory.record_step(
                db, trajectory=trajectory, action_type="process_result", query="generic scam offer"
            )
            await research_trajectory_memory.complete_step(
                db, step=bad, trajectory=trajectory, outcome=TrajectoryOutcome.MISLEADING, usefulness_score=0.1,
            )
            await db.commit()

            successful = await research_trajectory_memory.retrieve_successful_search_patterns(db)
            failed = await research_trajectory_memory.retrieve_failed_search_patterns(db)

            assert successful[0]["query"] == "daad scholarship"
            assert successful[0]["occurrences"] == 1
            assert failed[0]["query"] == "generic scam offer"

    @pytest.mark.asyncio
    async def test_get_source_performance_never_returns_a_trust_verdict(self, db_session_factory):
        """STEP 7: only real counts/timestamps -- no 'is_trustworthy'
        field anywhere in the real output."""
        async with db_session_factory() as db:
            trajectory = await research_trajectory_memory.start_trajectory(db, objective="Sweep")
            for outcome in (TrajectoryOutcome.SUCCESSFUL, TrajectoryOutcome.SUCCESSFUL, TrajectoryOutcome.FAILED):
                step = await research_trajectory_memory.record_step(
                    db, trajectory=trajectory, action_type="process_result", query="q",
                    source_domain="repeat-offender.example.com",
                )
                await research_trajectory_memory.complete_step(
                    db, step=step, trajectory=trajectory, outcome=outcome,
                )
            await db.commit()

            performance = await research_trajectory_memory.get_source_performance(db)
            entry = next(p for p in performance if p["source_domain"] == "repeat-offender.example.com")
            assert entry["total_steps_seen"] == 3
            assert entry["positive_signal_count"] == 2
            assert entry["negative_signal_count"] == 1
            assert set(entry.keys()) == {
                "source_domain", "total_steps_seen", "positive_signal_count",
                "negative_signal_count", "last_seen_at",
            }
            assert "is_trustworthy" not in entry
            assert "trust_score" not in entry

    @pytest.mark.asyncio
    async def test_build_research_hints_no_memory_case(self, db_session_factory):
        async with db_session_factory() as db:
            hints = await research_trajectory_memory.build_research_hints(
                db, objective="A genuinely brand-new research objective nothing has ever touched"
            )
            assert hints["successful_queries"] == []
            assert hints["failed_queries"] == []
            assert hints["useful_domains"] == []
            assert hints["low_value_domains"] == []
            assert hints["known_patterns"] == []
            assert len(hints["recommended_next_actions"]) == 1
            assert "no prior research memory" in hints["recommended_next_actions"][0].lower()

    @pytest.mark.asyncio
    async def test_build_research_hints_with_real_history(self, db_session_factory):
        async with db_session_factory() as db:
            trajectory = await research_trajectory_memory.start_trajectory(
                db, objective="Find scholarships for engineering students in Egypt"
            )
            step = await research_trajectory_memory.record_step(
                db, trajectory=trajectory, action_type="process_result",
                query="daad scholarship engineering", source_domain="daad.de",
            )
            await research_trajectory_memory.complete_step(
                db, step=step, trajectory=trajectory, outcome=TrajectoryOutcome.SUCCESSFUL,
                usefulness_score=0.9, discovered_opportunities_count=1,
            )
            await research_trajectory_memory.record_outcome(db, trajectory=trajectory, final_outcome=TrajectoryOutcome.SUCCESSFUL)
            await db.commit()

            hints = await research_trajectory_memory.build_research_hints(
                db, objective="Find scholarships for engineering students in Egypt"
            )
            assert "daad scholarship engineering" in hints["successful_queries"]
            assert "daad.de" in hints["useful_domains"]
            assert len(hints["known_patterns"]) == 1
            assert hints["known_patterns"][0]["final_outcome"] == TrajectoryOutcome.SUCCESSFUL.value
            assert len(hints["recommended_next_actions"]) >= 1


class TestIsolationAndPersistence:
    @pytest.mark.asyncio
    async def test_different_trajectories_are_isolated(self, db_session_factory):
        async with db_session_factory() as db:
            t1 = await research_trajectory_memory.start_trajectory(db, objective="Run A")
            t2 = await research_trajectory_memory.start_trajectory(db, objective="Run B")
            await research_trajectory_memory.record_step(db, trajectory=t1, action_type="query_source", query="qa")
            await research_trajectory_memory.record_step(db, trajectory=t2, action_type="query_source", query="qb")
            await db.commit()

            assert t1.total_steps == 1
            assert t2.total_steps == 1

            result = await db.execute(select(ResearchTrajectoryStep).where(ResearchTrajectoryStep.trajectory_id == t1.id))
            t1_steps = result.scalars().all()
            assert len(t1_steps) == 1
            assert t1_steps[0].query == "qa"

    @pytest.mark.asyncio
    async def test_persistence_across_a_fresh_session(self, db_engine):
        """Real persistence: write with one AsyncSession, commit, then
        read back with a brand-new, unrelated AsyncSession -- not just
        re-reading the same in-memory Python object."""
        session_factory = async_sessionmaker(bind=db_engine, class_=AsyncSession, expire_on_commit=False)

        async with session_factory() as db:
            trajectory = await research_trajectory_memory.start_trajectory(db, objective="Persisted run")
            await research_trajectory_memory.record_step(db, trajectory=trajectory, action_type="query_source", query="q")
            await research_trajectory_memory.record_outcome(db, trajectory=trajectory, final_outcome=TrajectoryOutcome.SUCCESSFUL)
            await db.commit()
            trajectory_id = trajectory.id

        async with session_factory() as fresh_db:
            reloaded = await fresh_db.get(ResearchTrajectory, trajectory_id)
            assert reloaded is not None
            assert reloaded.objective == "Persisted run"
            assert reloaded.status == TrajectoryStatus.COMPLETED.value

            result = await fresh_db.execute(
                select(ResearchTrajectoryStep).where(ResearchTrajectoryStep.trajectory_id == trajectory_id)
            )
            steps = result.scalars().all()
            assert len(steps) == 1

    @pytest.mark.asyncio
    async def test_status_transitions_running_to_completed(self, db_session_factory):
        async with db_session_factory() as db:
            trajectory = await research_trajectory_memory.start_trajectory(db, objective="Sweep")
            assert trajectory.status == TrajectoryStatus.RUNNING.value
            assert trajectory.completed_at is None

            await research_trajectory_memory.record_outcome(db, trajectory=trajectory, final_outcome=TrajectoryOutcome.SUCCESSFUL)
            await db.commit()

            assert trajectory.status == TrajectoryStatus.COMPLETED.value
            assert trajectory.completed_at is not None


# ---------------------------------------------------------------------
# Real pipeline integration (Step 5): `_run_daily_opportunity_scraper`
# (app/workers/aggregator_tasks.py) must create a real, completed
# trajectory with real steps. `process_raw_post_via_ai` is mocked at
# the same Gemini boundary `tests/test_aggregator.py` already
# establishes as this codebase's convention for that call.
# ---------------------------------------------------------------------


class TestAggregatorPipelineIntegration:
    @pytest.mark.asyncio
    async def test_daily_scraper_run_creates_a_real_trajectory_with_steps(self, db_session_factory):
        from app.schemas.opportunity import OpportunityCreate
        from app.models.enums import OpportunityType, SourceType as _SourceType
        from app.services.aggregator_service import SourcesConfig, TelegramChannelConfig
        from app.workers import aggregator_tasks

        fake_config = SourcesConfig(
            telegram_channels=[TelegramChannelConfig(handle="@testchannel", category="Scholarships")],
            facebook_pages=[],
        )

        def _fake_extraction(raw_text, source_info):
            return OpportunityCreate(
                title="Real Test Scholarship",
                target_url="https://example.com/scholarship-1",
                opportunity_type=OpportunityType.SCHOLARSHIP,
                country="Egypt",
                is_local_egypt=True,
                source_name=source_info["source_name"],
                source_type=source_info["source_type"],
                source_base_url=source_info["source_base_url"],
            )

        with patch("app.workers.aggregator_tasks.AsyncSessionLocal", db_session_factory), patch(
            "app.workers.aggregator_tasks.engine", new=AsyncMock()
        ), patch("app.workers.aggregator_tasks.load_sources_config", return_value=fake_config), patch(
            "app.workers.aggregator_tasks.fetch_new_telegram_posts", return_value=["some raw post text"]
        ), patch(
            "app.workers.aggregator_tasks.process_raw_post_via_ai", new=AsyncMock(side_effect=_fake_extraction)
        ):
            summary = await aggregator_tasks._run_daily_opportunity_scraper()

        assert summary["status"] == "completed"
        assert summary["created"] == 1

        async with db_session_factory() as db:
            result = await db.execute(select(ResearchTrajectory))
            trajectories = result.scalars().all()
            assert len(trajectories) == 1
            trajectory = trajectories[0]
            assert trajectory.status == TrajectoryStatus.COMPLETED.value
            assert "telegram channel(s)" in trajectory.objective
            assert trajectory.opportunities_discovered == 1
            assert trajectory.useful_sources == ["https://t.me/testchannel"]

            steps_result = await db.execute(
                select(ResearchTrajectoryStep)
                .where(ResearchTrajectoryStep.trajectory_id == trajectory.id)
                .order_by(ResearchTrajectoryStep.step_number.asc())
            )
            steps = steps_result.scalars().all()
            # Deliverable 4.2 (Adaptive Discovery Execution) adds one
            # real `discovery_round` step per bounded round, recorded
            # AFTER that round's real source-polls, for round-level
            # planning/stop/escalation observability (see
            # `app/workers/aggregator_tasks.py::_run_adaptive_discovery_rounds`)
            # -- disclosed, deliberate step-count change from the
            # pre-D4.2 "1 query_source + 1 process_result" shape.
            assert len(steps) == 3  # 1 query_source + 1 process_result + 1 discovery_round
            assert steps[0].action_type == "query_source"
            assert steps[0].query == "@testchannel"
            assert steps[1].action_type == "process_result"
            assert steps[1].parent_step_id == steps[0].id
            assert steps[1].reference_id is not None
            assert steps[1].discovered_opportunities_count == 1
            assert steps[2].action_type == "discovery_round"

    @pytest.mark.asyncio
    async def test_daily_scraper_run_records_failed_steps_on_extraction_error(self, db_session_factory):
        from app.services.aggregator_service import AggregatorError, SourcesConfig, TelegramChannelConfig
        from app.workers import aggregator_tasks

        fake_config = SourcesConfig(
            telegram_channels=[TelegramChannelConfig(handle="@testchannel", category="Scholarships")],
            facebook_pages=[],
        )

        with patch("app.workers.aggregator_tasks.AsyncSessionLocal", db_session_factory), patch(
            "app.workers.aggregator_tasks.engine", new=AsyncMock()
        ), patch("app.workers.aggregator_tasks.load_sources_config", return_value=fake_config), patch(
            "app.workers.aggregator_tasks.fetch_new_telegram_posts", return_value=["bad post"]
        ), patch(
            "app.workers.aggregator_tasks.process_raw_post_via_ai",
            new=AsyncMock(side_effect=AggregatorError("Gemini call failed")),
        ):
            summary = await aggregator_tasks._run_daily_opportunity_scraper()

        assert summary["errors"] == 1

        async with db_session_factory() as db:
            result = await db.execute(select(ResearchTrajectory))
            trajectory = result.scalars().first()
            assert trajectory.final_outcome == TrajectoryOutcome.FAILED.value
            assert trajectory.failed_steps == 1

            steps_result = await db.execute(
                select(ResearchTrajectoryStep).where(ResearchTrajectoryStep.action_type == "process_result")
            )
            failed_step = steps_result.scalars().first()
            assert failed_step.outcome == TrajectoryOutcome.FAILED.value
            assert failed_step.error_reason is not None


# ---------------------------------------------------------------------
# API layer (real HTTP)
# ---------------------------------------------------------------------


async def _make_user(db: AsyncSession, *, admin_role: AdminRole | None = None) -> User:
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.com", full_name="Test User", admin_role=admin_role)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@pytest.fixture
def client_factory(db_session_factory):
    def _build(user: User | None) -> TestClient:
        async def _override_get_db():
            async with db_session_factory() as session:
                yield session

        app.dependency_overrides[get_db] = _override_get_db

        if user is not None:
            async def _override_get_current_user():
                async with db_session_factory() as session:
                    return await session.get(User, user.id)

            app.dependency_overrides[get_current_user] = _override_get_current_user

        return TestClient(app)

    yield _build
    app.dependency_overrides.clear()


class TestAdminResearchEndpoints:
    @pytest.mark.asyncio
    async def test_plain_student_gets_403_on_hints(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            student = await _make_user(db, admin_role=None)
        client = client_factory(student)
        res = client.get("/api/v1/admin/research/hints", params={"objective": "find scholarships"})
        assert res.status_code == 403

    @pytest.mark.asyncio
    async def test_unknown_trajectory_is_404(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        res = client.get(f"/api/v1/admin/research/trajectories/{uuid.uuid4()}")
        assert res.status_code == 404

    @pytest.mark.asyncio
    async def test_real_trajectory_round_trips_over_http(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
            trajectory = await research_trajectory_memory.start_trajectory(db, objective="HTTP round-trip test")
            await research_trajectory_memory.record_step(
                db, trajectory=trajectory, action_type="query_source", query="q1", source_domain="example.com"
            )
            await research_trajectory_memory.record_outcome(db, trajectory=trajectory, final_outcome=TrajectoryOutcome.SUCCESSFUL)
            await db.commit()
            trajectory_id = trajectory.id

        client = client_factory(moderator)
        res = client.get(f"/api/v1/admin/research/trajectories/{trajectory_id}")
        assert res.status_code == 200
        body = res.json()
        assert body["objective"] == "HTTP round-trip test"
        assert body["status"] == "completed"
        assert len(body["steps"]) == 1
        assert body["steps"][0]["query"] == "q1"

    @pytest.mark.asyncio
    async def test_hints_endpoint_returns_the_documented_shape(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        res = client.get("/api/v1/admin/research/hints", params={"objective": "brand new objective"})
        assert res.status_code == 200
        body = res.json()
        assert set(body.keys()) == {
            "successful_queries", "failed_queries", "useful_domains",
            "low_value_domains", "recommended_next_actions", "known_patterns",
        }

    @pytest.mark.asyncio
    async def test_source_performance_endpoint(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
            trajectory = await research_trajectory_memory.start_trajectory(db, objective="Sweep")
            step = await research_trajectory_memory.record_step(
                db, trajectory=trajectory, action_type="process_result", query="q", source_domain="example.com"
            )
            await research_trajectory_memory.complete_step(
                db, step=step, trajectory=trajectory, outcome=TrajectoryOutcome.SUCCESSFUL,
            )
            await db.commit()

        client = client_factory(moderator)
        res = client.get("/api/v1/admin/research/source-performance")
        assert res.status_code == 200
        body = res.json()
        assert any(p["source_domain"] == "example.com" for p in body)
        assert "is_trustworthy" not in body[0]
