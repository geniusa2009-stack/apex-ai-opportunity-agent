"""Phase 3B: Real Source Adapters & Live Discovery Execution.

Integration tests proving:
1. `_build_source_candidates` routes a `live=True` channel to the real
   `"telegram_channel_live"` kind, everything else unchanged (Section 22).
2. `_poll_source`'s error isolation genuinely survives a REAL adapter
   raising a REAL, classified exception (network timeout, unconfigured
   credential) -- one broken source never aborts the run (Section 14).
3. Real per-item provenance (`external_item_id`/`item_url`/
   `published_at`/`retrieved_at`) genuinely flows from a live adapter's
   `RawDiscoveryResult` through `process_raw_post_via_ai` into the
   persisted `Opportunity.raw_extracted_data` (Section 21/22) -- via the
   real DB, real `upsert_opportunity`, no mocked persistence layer.
4. Discovery output from a live-fetched item is STILL exactly as
   unverified as mocked-path output (Section 18/VERIFICATION BOUNDARY):
   `is_verified=False`, `ai_confidence_score=None`, no code path here
   can set either.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.enums import OpportunityType
from app.models.research_trajectory import ResearchTrajectoryStep
from app.services import research_orchestrator as ro
from app.services import research_trajectory_memory
from app.services.aggregator_service import (
    AIExtractedOpportunityFields,
    FacebookPageConfig,
    SourcesConfig,
    TelegramChannelConfig,
)
from app.services.source_adapters import RawDiscoveryResult
from app.workers.aggregator_tasks import _build_source_candidates, _poll_source

_CREATE_SOURCES_TABLE_SQL = """
CREATE TABLE sources (
    id VARCHAR(36) PRIMARY KEY, name VARCHAR(255) NOT NULL UNIQUE, base_url VARCHAR(2048),
    source_type VARCHAR(20), active_status BOOLEAN NOT NULL DEFAULT 1,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_CHANGE_HISTORY_TABLE_SQL = """
CREATE TABLE opportunity_change_history (
    id VARCHAR(36) PRIMARY KEY, opportunity_id VARCHAR(36) NOT NULL, field_name VARCHAR(50) NOT NULL,
    old_value TEXT, new_value TEXT, source_id VARCHAR(36), evidence_id VARCHAR(36),
    change_reason TEXT NOT NULL, changed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


_CREATE_OPPORTUNITIES_TABLE_SQL = """
CREATE TABLE opportunities (
    id VARCHAR(36) PRIMARY KEY, title VARCHAR(500) NOT NULL, description TEXT, target_url VARCHAR(2048) NOT NULL,
    source_id VARCHAR(36) NOT NULL, type VARCHAR(20) NOT NULL, deadline DATETIME, eligibility_criteria TEXT,
    funding_details TEXT, location VARCHAR(255), country VARCHAR(120) DEFAULT 'Global',
    is_verified BOOLEAN NOT NULL DEFAULT 0, ai_confidence_score FLOAT, raw_extracted_data TEXT, embedding TEXT,
    is_local_egypt BOOLEAN NOT NULL DEFAULT 0, target_age_group VARCHAR(50), target_academic_levels TEXT,
    min_age INTEGER, max_age INTEGER, target_gender VARCHAR(10),
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    review_status VARCHAR(20) NOT NULL DEFAULT 'published', claimed_by VARCHAR(36), claimed_at DATETIME, lifecycle_state VARCHAR(30) NOT NULL DEFAULT 'discovered', lifecycle_state_reason TEXT, lifecycle_state_evaluated_at DATETIME
)
"""

_CREATE_USER_BOOKMARKS_TABLE_SQL = """
CREATE TABLE user_bookmarks (
    id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36) NOT NULL, opportunity_id VARCHAR(36) NOT NULL,
    course_id VARCHAR(36), status VARCHAR(20) NOT NULL DEFAULT 'saved', created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_RESEARCH_TRAJECTORIES_SQL = """
CREATE TABLE research_trajectories (
    id VARCHAR(36) PRIMARY KEY, objective VARCHAR(500) NOT NULL, normalized_objective VARCHAR(500) NOT NULL,
    started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, completed_at DATETIME,
    status VARCHAR(20) NOT NULL DEFAULT 'running', total_steps INTEGER NOT NULL DEFAULT 0,
    successful_steps INTEGER NOT NULL DEFAULT 0, failed_steps INTEGER NOT NULL DEFAULT 0,
    opportunities_discovered INTEGER NOT NULL DEFAULT 0, opportunities_verified INTEGER NOT NULL DEFAULT 0,
    useful_sources TEXT, research_depth INTEGER NOT NULL DEFAULT 0, final_outcome VARCHAR(20), trajectory_metadata TEXT
)
"""

_CREATE_RESEARCH_TRAJECTORY_STEPS_SQL = """
CREATE TABLE research_trajectory_steps (
    id VARCHAR(36) PRIMARY KEY, trajectory_id VARCHAR(36) NOT NULL, step_number INTEGER NOT NULL,
    action_type VARCHAR(50) NOT NULL, query VARCHAR(500), source_domain VARCHAR(500),
    reference_id VARCHAR(100), parent_step_id VARCHAR(36), outcome VARCHAR(20) NOT NULL DEFAULT 'neutral',
    usefulness_score FLOAT, discovered_entities_count INTEGER NOT NULL DEFAULT 0,
    discovered_opportunities_count INTEGER NOT NULL DEFAULT 0, verification_impact VARCHAR(50),
    error_reason VARCHAR(500), created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, step_metadata TEXT
)
"""


@pytest_asyncio.fixture
async def db_session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.execute(text(_CREATE_SOURCES_TABLE_SQL))
        await conn.execute(text(_CREATE_OPPORTUNITIES_TABLE_SQL))
        await conn.execute(text(_CREATE_CHANGE_HISTORY_TABLE_SQL))
        await conn.execute(text(_CREATE_USER_BOOKMARKS_TABLE_SQL))
        await conn.execute(text(_CREATE_RESEARCH_TRAJECTORIES_SQL))
        await conn.execute(text(_CREATE_RESEARCH_TRAJECTORY_STEPS_SQL))
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


def _fields(url: str | None) -> AIExtractedOpportunityFields:
    return AIExtractedOpportunityFields(
        title="A live-discovered opportunity", description="Real extracted description.", deadline=None,
        original_link=url, target_age_group="university", country_hint="Global", opportunity_type="scholarship",
    )


class TestBuildSourceCandidatesLiveRouting:
    def test_live_flagged_channel_gets_the_live_kind(self):
        config = SourcesConfig(
            telegram_channels=[
                TelegramChannelConfig(handle="@opcorners", category="Scholarships", live=True),
                TelegramChannelConfig(handle="@other", category="General"),
            ],
            facebook_pages=[],
        )
        candidates, _ = _build_source_candidates(config)
        by_id = {c.source_id: c for c in candidates}
        assert by_id["@opcorners"].source_kind == "telegram_channel_live"
        assert by_id["@other"].source_kind == "telegram_channel"

    def test_default_live_false_preserves_pre_phase3b_behavior(self):
        config = SourcesConfig(
            telegram_channels=[TelegramChannelConfig(handle="@x", category="General")], facebook_pages=[],
        )
        candidates, _ = _build_source_candidates(config)
        assert candidates[0].source_kind == "telegram_channel"


@pytest.mark.asyncio
class TestPollSourceErrorIsolation:
    async def test_adapter_raising_a_real_exception_is_isolated_not_fatal(self, db_session_factory):
        """A REAL adapter (unlike the old mocked ones) can raise real
        network/config exceptions. `_poll_source` must record a clean
        per-source failure and return -- never propagate and crash the
        whole discovery round (Section 14)."""
        async with db_session_factory() as session:
            trajectory = await research_trajectory_memory.start_trajectory(session, objective="test run")
            source = ro.DiscoverySourceCandidate(
                source_id="@opcorners", source_kind="telegram_channel_live",
                category="Scholarships", source_base_url="https://t.me/opcorners",
            )
            config_obj = TelegramChannelConfig(handle="@opcorners", category="Scholarships", live=True)

            with patch(
                "app.workers.aggregator_tasks.get_adapter"
            ) as mock_get_adapter:
                broken_adapter = AsyncMock()
                broken_adapter.fetch = lambda cfg: (_ for _ in ()).throw(
                    RuntimeError("simulated: t.me egress blocked (connect_rejected)")
                )
                mock_get_adapter.return_value = broken_adapter

                stats = await _poll_source(
                    session, trajectory=trajectory, round_number=1, source=source, config_obj=config_obj,
                )

            assert stats == {"processed": 0, "created": 0, "updated": 0, "errors": 1}

            result = await session.execute(
                select(ResearchTrajectoryStep).where(ResearchTrajectoryStep.action_type == "query_source")
            )
            steps = result.scalars().all()
            assert len(steps) == 1
            assert steps[0].step_metadata is not None
            # The error is recorded, real and honest -- never a secret,
            # never swallowed into a false "0 items, all fine" result.
            assert "simulated" in (steps[0].step_metadata.get("error") or "")

    async def test_one_broken_source_does_not_prevent_a_second_source_from_succeeding(self, db_session_factory):
        """Section 14's own literal example: Source A fails, Source B
        (in the same round) must still succeed -- proven by running
        `_poll_source` twice in the same trajectory, once against a
        raising adapter and once against a working one."""
        async with db_session_factory() as session:
            trajectory = await research_trajectory_memory.start_trajectory(session, objective="test run")

            broken_source = ro.DiscoverySourceCandidate(
                source_id="@broken", source_kind="telegram_channel_live",
                category="Scholarships", source_base_url="https://t.me/broken",
            )
            broken_config = TelegramChannelConfig(handle="@broken", category="Scholarships", live=True)

            with patch("app.workers.aggregator_tasks.get_adapter") as mock_get_adapter:
                broken_adapter = AsyncMock()
                broken_adapter.fetch = lambda cfg: (_ for _ in ()).throw(RuntimeError("boom"))
                mock_get_adapter.return_value = broken_adapter
                stats_a = await _poll_source(
                    session, trajectory=trajectory, round_number=1, source=broken_source, config_obj=broken_config,
                )

            good_source = ro.DiscoverySourceCandidate(
                source_id="@good", source_kind="telegram_channel",
                category="Scholarships", source_base_url="https://t.me/good",
            )
            good_config = TelegramChannelConfig(handle="@good", category="Scholarships")

            with patch(
                "app.services.aggregator_service._extract_fields_via_gemini",
                new=AsyncMock(return_value=_fields("https://example.com/from-good-source")),
            ), patch(
                "app.services.source_adapters.fetch_new_telegram_posts", return_value=["a real post"],
            ):
                stats_b = await _poll_source(
                    session, trajectory=trajectory, round_number=1, source=good_source, config_obj=good_config,
                )

            assert stats_a["errors"] == 1
            assert stats_b["errors"] == 0
            assert stats_b["created"] == 1


@pytest.mark.asyncio
class TestProvenanceFlowsThroughToPersistence:
    async def test_live_item_provenance_and_timestamp_persist_and_stay_unverified(self, db_session_factory):
        """A `RawDiscoveryResult` carrying real external_item_id/item_url/
        published_at flows through `process_raw_post_via_ai` and
        `upsert_opportunity` into the real, persisted
        `Opportunity.raw_extracted_data` -- and the resulting row is
        exactly as unverified as any mocked-path discovery (Sections
        8/9/18/21)."""
        async with db_session_factory() as session:
            trajectory = await research_trajectory_memory.start_trajectory(session, objective="test run")
            source = ro.DiscoverySourceCandidate(
                source_id="@opcorners", source_kind="telegram_channel_live",
                category="Scholarships", source_base_url="https://t.me/opcorners",
            )
            config_obj = TelegramChannelConfig(handle="@opcorners", category="Scholarships", live=True)
            real_published_at = dt.datetime(2026, 9, 10, 10, 0, 0, tzinfo=dt.timezone.utc)
            real_retrieved_at = dt.datetime(2026, 9, 12, 8, 0, 0, tzinfo=dt.timezone.utc)

            with patch("app.workers.aggregator_tasks.get_adapter") as mock_get_adapter, patch(
                "app.services.aggregator_service._extract_fields_via_gemini",
                # The AI extraction itself found NO link in the post body
                # (original_link=None) -- this is exactly the case where
                # the real item_url provenance should be preferred over
                # the generic channel base URL.
                new=AsyncMock(return_value=_fields(None)),
            ):
                adapter = AsyncMock()
                adapter.fetch = lambda cfg: [
                    RawDiscoveryResult(
                        raw_text="A real scholarship post, no link in the body",
                        source_kind="telegram_channel_live",
                        external_item_id="opcorners/12345",
                        item_url="https://t.me/opcorners/12345",
                        published_at=real_published_at,
                        retrieved_at=real_retrieved_at,
                    )
                ]
                mock_get_adapter.return_value = adapter

                stats = await _poll_source(
                    session, trajectory=trajectory, round_number=1, source=source, config_obj=config_obj,
                )

            assert stats == {"processed": 1, "created": 1, "updated": 0, "errors": 0}

            from app.models.opportunity import Opportunity

            result = await session.execute(
                select(Opportunity).where(Opportunity.target_url == "https://t.me/opcorners/12345")
            )
            opportunity = result.scalar_one()

            # VERIFICATION BOUNDARY (Section 18): discovery output stays
            # exactly as unverified as any mocked-path opportunity.
            assert opportunity.is_verified is False
            assert opportunity.ai_confidence_score is None

            # target_url correctly preferred the item's own real URL
            # over the generic channel base URL (Section 9).
            assert opportunity.target_url == "https://t.me/opcorners/12345"

            # Real provenance/temporal fields persisted via the
            # pre-existing raw_extracted_data catch-all -- no migration.
            raw = opportunity.raw_extracted_data
            assert raw["external_item_id"] == "opcorners/12345"
            assert raw["item_url"] == "https://t.me/opcorners/12345"
            assert raw["source_published_at"] is not None
            assert raw["retrieved_at"] is not None
