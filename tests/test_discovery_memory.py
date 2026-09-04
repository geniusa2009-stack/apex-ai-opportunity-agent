"""Tests for D3 backward-looking discovery memory (Deliverable 5,
Feature 3 / `research_trajectory_memory.discovery_source_yield_hints`),
and its effect on `plan_discovery_round`'s real ordering
(`research_orchestrator.py`).
"""
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.enums import TrajectoryOutcome
from app.services import research_orchestrator as ro
from app.services import research_trajectory_memory

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


@pytest_asyncio.fixture
async def db_session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.execute(text(_CREATE_RESEARCH_TRAJECTORIES_SQL))
        await conn.execute(text(_CREATE_RESEARCH_TRAJECTORY_STEPS_SQL))
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


async def _seed_process_result_steps(db: AsyncSession, *, label: str, outcomes_and_counts: list[tuple[str, int]]):
    """Seeds one `query_source` + N `process_result` steps for `label`,
    same real shape `_poll_source` produces -- `outcomes_and_counts` is
    a list of (TrajectoryOutcome value, discovered_opportunities_count)."""
    trajectory = await research_trajectory_memory.start_trajectory(db, objective=f"seed for {label}")
    query_step = await research_trajectory_memory.record_step(
        db, trajectory=trajectory, action_type="query_source", query=label, source_domain=f"https://example.com/{label}"
    )
    for outcome_value, discovered in outcomes_and_counts:
        step = await research_trajectory_memory.record_step(
            db, trajectory=trajectory, action_type="process_result", query=label, parent_step_id=query_step.id
        )
        await research_trajectory_memory.complete_step(
            db, step=step, trajectory=trajectory, outcome=TrajectoryOutcome(outcome_value),
            discovered_opportunities_count=discovered,
        )
    await db.commit()


@pytest.mark.asyncio
class TestDiscoverySourceYieldHints:
    async def test_no_history_behaves_normally(self, db_session_factory):
        async with db_session_factory() as db:
            hints = await research_trajectory_memory.discovery_source_yield_hints(db)
            assert hints["source_stats"] == []
            assert hints["high_yield_source_labels"] == set()
            assert hints["low_yield_source_labels"] == set()

    async def test_high_yield_source_is_flagged(self, db_session_factory):
        async with db_session_factory() as db:
            # 4 processed, 3 created (new) -- yield_rate 0.75 >= 0.5, sample >= 3.
            await _seed_process_result_steps(
                db, label="@good_channel",
                outcomes_and_counts=[("neutral", 1), ("neutral", 1), ("neutral", 1), ("neutral", 0)],
            )
            hints = await research_trajectory_memory.discovery_source_yield_hints(db)
            assert "@good_channel" in hints["high_yield_source_labels"]
            assert "@good_channel" not in hints["low_yield_source_labels"]

    async def test_duplicate_heavy_source_is_deprioritized(self, db_session_factory):
        async with db_session_factory() as db:
            # 4 processed, 0 created -- duplicate_rate 1.0 >= 0.7.
            await _seed_process_result_steps(
                db, label="@dup_channel",
                outcomes_and_counts=[("neutral", 0), ("neutral", 0), ("neutral", 0), ("neutral", 0)],
            )
            hints = await research_trajectory_memory.discovery_source_yield_hints(db)
            assert "@dup_channel" in hints["low_yield_source_labels"]
            assert "@dup_channel" not in hints["high_yield_source_labels"]

    async def test_small_sample_is_not_judged(self, db_session_factory):
        async with db_session_factory() as db:
            # Only 1 processed -- below min_sample=3, must not be flagged either way.
            await _seed_process_result_steps(db, label="@new_channel", outcomes_and_counts=[("neutral", 1)])
            hints = await research_trajectory_memory.discovery_source_yield_hints(db)
            assert "@new_channel" not in hints["high_yield_source_labels"]
            assert "@new_channel" not in hints["low_yield_source_labels"]


class TestPlanDiscoveryRoundHonorsHistoricalHints:
    def _policy(self):
        return ro.DepthPolicy(
            max_research_steps=3, max_provider_calls=3, consult_trajectory_memory=True,
            require_extra_verification=False, contradiction_hunting=False, allow_early_stop=True,
            max_escalation_depth=1,
        )

    def _sources(self):
        return [
            ro.DiscoverySourceCandidate(source_id="@alpha", source_kind="telegram_channel", category=None, source_base_url="https://t.me/alpha"),
            ro.DiscoverySourceCandidate(source_id="@beta", source_kind="telegram_channel", category=None, source_base_url="https://t.me/beta"),
            ro.DiscoverySourceCandidate(source_id="@gamma", source_kind="telegram_channel", category=None, source_base_url="https://t.me/gamma"),
        ]

    def test_high_yield_source_moves_to_front(self):
        policy = self._policy()
        plan = ro.plan_discovery_round(
            round_number=1,
            all_sources=self._sources(),
            already_polled_source_ids=set(),
            depth_policy=policy,
            priority_source_ids={"gamma"},  # normalized (no leading @)
        )
        assert plan.sources_to_poll[0].source_id == "@gamma"
        assert "historically high-yield" in plan.priority_reasons["@gamma"]

    def test_low_yield_source_moves_to_back(self):
        policy = self._policy()
        plan = ro.plan_discovery_round(
            round_number=1,
            all_sources=self._sources(),
            already_polled_source_ids=set(),
            depth_policy=policy,
            deprioritized_source_ids={"alpha"},
        )
        ids = [s.source_id for s in plan.sources_to_poll]
        assert ids.index("@alpha") == len(ids) - 1
        assert "duplicate-heavy" in plan.priority_reasons["@alpha"]

    def test_no_hints_preserves_default_order(self):
        policy = self._policy()
        plan = ro.plan_discovery_round(
            round_number=1,
            all_sources=self._sources(),
            already_polled_source_ids=set(),
            depth_policy=policy,
        )
        assert [s.source_id for s in plan.sources_to_poll] == ["@alpha", "@beta", "@gamma"]
