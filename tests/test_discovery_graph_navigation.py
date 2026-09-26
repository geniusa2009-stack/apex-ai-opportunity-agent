"""Tests for D2 real graph navigation (Deliverable 5, Feature 2 /
`research_orchestrator.discovery_graph_navigation`), and its effect on
discovery priority via `_priority_opportunity_types`
(`app/workers/aggregator_tasks.py`).

Same in-memory SQLite fixture technique as `tests/test_entity_relationships.py`.
"""
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.entity_relationship import EntityRelationship
from app.models.enums import RelationshipType
from app.models.opportunity import Opportunity
from app.services import research_orchestrator as ro

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
    review_status VARCHAR(20) NOT NULL DEFAULT 'published',
    claimed_by VARCHAR(36),
    claimed_at DATETIME, lifecycle_state VARCHAR(30) NOT NULL DEFAULT 'discovered', lifecycle_state_reason TEXT, lifecycle_state_evaluated_at DATETIME, monitoring_last_checked_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
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


@pytest_asyncio.fixture
async def db_session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.execute(text(_CREATE_SOURCES_TABLE_SQL))
        await conn.execute(text(_CREATE_OPPORTUNITIES_TABLE_SQL))
        await conn.execute(text(_CREATE_ENTITY_RELATIONSHIPS_TABLE_SQL))
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


async def _make_source(db: AsyncSession, name="Test Source") -> uuid.UUID:
    source_id = uuid.uuid4()
    await db.execute(
        text("INSERT INTO sources (id, name, base_url) VALUES (:id, :name, 'https://example.com')"),
        {"id": source_id.hex, "name": name},
    )
    await db.commit()
    return source_id


async def _make_opportunity(
    db: AsyncSession, *, source_id: uuid.UUID, country: str | None, opp_type: str = "scholarship", title="Opp"
) -> uuid.UUID:
    opp_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO opportunities (id, title, target_url, source_id, type, country) "
            "VALUES (:id, :title, 'https://example.com/apply', :source_id, :type, :country)"
        ),
        {"id": opp_id.hex, "title": title, "source_id": source_id.hex, "type": opp_type, "country": country},
    )
    await db.commit()
    return opp_id


async def _make_edge(db: AsyncSession, *, opp_id, related_id, relationship_type):
    db.add(
        EntityRelationship(opportunity_id=opp_id, related_opportunity_id=related_id, relationship_type=relationship_type)
    )
    await db.commit()


@pytest.mark.asyncio
class TestDiscoveryGraphNavigation:
    async def test_empty_graph_returns_real_zeros(self, db_session_factory):
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            anchor = await _make_opportunity(db, source_id=source_id, country="Egypt")
            result = await ro.discovery_graph_navigation(db, reference_opportunity_ids=[anchor])
            assert result["neighbor_countries"] == {}
            assert result["neighbor_entities"] == {}
            assert result["neighbor_opportunity_types"] == {}
            assert result["relationship_counts"] == {}
            assert result["reasons"]

    async def test_no_anchors_returns_real_empty(self, db_session_factory):
        async with db_session_factory() as db:
            result = await ro.discovery_graph_navigation(db, reference_opportunity_ids=[])
            assert result["anchor_count"] == 0
            assert result["neighbor_countries"] == {}

    async def test_real_neighboring_entities_and_countries(self, db_session_factory):
        async with db_session_factory() as db:
            org_a = await _make_source(db, "Org A")
            org_b = await _make_source(db, "Org B")
            anchor = await _make_opportunity(db, source_id=org_a, country="Egypt", opp_type="scholarship")
            same_org = await _make_opportunity(db, source_id=org_a, country="Jordan", opp_type="internship")
            same_country = await _make_opportunity(db, source_id=org_b, country="Egypt", opp_type="course")

            await _make_edge(db, opp_id=anchor, related_id=same_org, relationship_type=RelationshipType.SAME_ORGANIZATION.value)
            await _make_edge(db, opp_id=anchor, related_id=same_country, relationship_type=RelationshipType.SAME_COUNTRY.value)

            result = await ro.discovery_graph_navigation(db, reference_opportunity_ids=[anchor])

            assert result["neighbor_entities"] == {"Org A": 1}
            assert result["neighbor_countries"] == {"Egypt": 1}
            assert result["neighbor_opportunity_types"] == {"internship": 1, "course": 1}
            assert result["relationship_counts"] == {
                RelationshipType.SAME_ORGANIZATION.value: 1,
                RelationshipType.SAME_COUNTRY.value: 1,
            }

    async def test_unrelated_graph_does_not_leak_into_navigation(self, db_session_factory):
        async with db_session_factory() as db:
            org = await _make_source(db)
            anchor = await _make_opportunity(db, source_id=org, country="Egypt")
            # Two unrelated opportunities with no edges to the anchor at all.
            await _make_opportunity(db, source_id=org, country="Nowhere", opp_type="volunteer")
            result = await ro.discovery_graph_navigation(db, reference_opportunity_ids=[anchor])
            assert result["neighbor_countries"] == {}
            assert "Nowhere" not in result["neighbor_countries"]


@pytest.mark.asyncio
class TestGraphSignalChangesDiscoveryPriority:
    async def test_under_connected_type_is_prioritized(self, db_session_factory):
        from app.workers.aggregator_tasks import _priority_opportunity_types

        async with db_session_factory() as db:
            org = await _make_source(db)
            anchor = await _make_opportunity(db, source_id=org, country="Egypt", opp_type="scholarship")
            # Many neighbors of type "course", zero of type "volunteer" --
            # volunteer is real, honestly under-connected in this graph.
            for i in range(5):
                neighbor = await _make_opportunity(db, source_id=org, country="Egypt", opp_type="course", title=f"n{i}")
                await _make_edge(db, opp_id=anchor, related_id=neighbor, relationship_type=RelationshipType.SAME_COUNTRY.value)

            priority_types = await _priority_opportunity_types(db, gapped_types=set())
            assert "volunteer" in priority_types
            assert "course" not in priority_types

    async def test_no_graph_signal_leaves_gapped_types_unchanged(self, db_session_factory):
        from app.workers.aggregator_tasks import _priority_opportunity_types

        async with db_session_factory() as db:
            priority_types = await _priority_opportunity_types(db, gapped_types={"internship"})
            assert priority_types == {"internship"}
