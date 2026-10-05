"""Behavioral tests for the Simplified Knowledge Graph (Deliverable 2):

- `entity_relationship_service.sync_relationships_for_opportunity`: real,
  incremental, bidirectional edge creation from real `source_id`/
  `country` matches; idempotent on a second call; respects the real
  pairwise-cap scale boundary.
- `entity_relationship_service.rebuild_all_relationships`: real, full,
  idempotent rebuild from the real `Opportunity` table.
- `GET /recommendations/{opportunity_id}/related`: real HTTP layer,
  real 404 for an unknown opportunity, real PUBLISHED-only gating, real
  edges returned with their real `relationship_type`.

Real HTTP layer via `TestClient` + `get_db` dependency override,
in-memory SQLite -- same established technique as
`tests/test_opportunity_review.py`.
"""

import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.session import get_db
from app.main import app
from app.models.entity_relationship import EntityRelationship
from app.models.enums import RelationshipType
from app.models.opportunity import Opportunity
from app.services import entity_relationship_service

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
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.execute(text(_CREATE_SOURCES_TABLE_SQL))
        await conn.execute(text(_CREATE_USER_BOOKMARKS_TABLE_SQL))
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


async def _make_opportunity(db: AsyncSession, *, source_id: uuid.UUID, country: str | None, title="Opp") -> uuid.UUID:
    opp_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO opportunities (id, title, target_url, source_id, type, country) "
            "VALUES (:id, :title, 'https://example.com/apply', :source_id, 'scholarship', :country)"
        ),
        {"id": opp_id.hex, "title": title, "source_id": source_id.hex, "country": country},
    )
    await db.commit()
    return opp_id


class TestSyncRelationshipsForOpportunity:
    @pytest.mark.asyncio
    async def test_creates_bidirectional_same_organization_edges(self, db_session_factory):
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            opp_a = await _make_opportunity(db, source_id=source_id, country="Egypt", title="A")
            opp_b = await _make_opportunity(db, source_id=source_id, country="Jordan", title="B")

            a = await db.get(Opportunity, opp_a)
            written = await entity_relationship_service.sync_relationships_for_opportunity(db, a)
            await db.commit()

            # 2 edges for SAME_ORGANIZATION (A->B, B->A); country differs
            # so no SAME_COUNTRY edges.
            assert written == 2

            result = await db.execute(select(EntityRelationship))
            edges = result.scalars().all()
            assert len(edges) == 2
            types = {e.relationship_type for e in edges}
            assert types == {RelationshipType.SAME_ORGANIZATION.value}
            pairs = {(e.opportunity_id, e.related_opportunity_id) for e in edges}
            assert pairs == {(opp_a, opp_b), (opp_b, opp_a)}

    @pytest.mark.asyncio
    async def test_creates_same_country_edges_too(self, db_session_factory):
        async with db_session_factory() as db:
            source_1 = await _make_source(db, "Source 1")
            source_2 = await _make_source(db, "Source 2")
            opp_a = await _make_opportunity(db, source_id=source_1, country="Egypt", title="A")
            opp_b = await _make_opportunity(db, source_id=source_2, country="Egypt", title="B")

            a = await db.get(Opportunity, opp_a)
            written = await entity_relationship_service.sync_relationships_for_opportunity(db, a)
            await db.commit()

            assert written == 2
            result = await db.execute(select(EntityRelationship))
            edges = result.scalars().all()
            assert {e.relationship_type for e in edges} == {RelationshipType.SAME_COUNTRY.value}

    @pytest.mark.asyncio
    async def test_idempotent_on_second_call(self, db_session_factory):
        """Calling this twice for the same opportunity must not create
        duplicate edges -- real re-verification (e.g. a re-run of the
        verification task) must not blow up the graph."""
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            opp_a = await _make_opportunity(db, source_id=source_id, country="Egypt", title="A")
            opp_b = await _make_opportunity(db, source_id=source_id, country="Egypt", title="B")

            a = await db.get(Opportunity, opp_a)
            await entity_relationship_service.sync_relationships_for_opportunity(db, a)
            await db.commit()
            first_count = (await db.execute(select(EntityRelationship))).scalars().all()

            a2 = await db.get(Opportunity, opp_a)
            await entity_relationship_service.sync_relationships_for_opportunity(db, a2)
            await db.commit()
            second_count = (await db.execute(select(EntityRelationship))).scalars().all()

            assert len(first_count) == len(second_count)

    @pytest.mark.asyncio
    async def test_no_edges_when_nothing_matches(self, db_session_factory):
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            opp_a = await _make_opportunity(db, source_id=source_id, country="Egypt", title="A")

            a = await db.get(Opportunity, opp_a)
            written = await entity_relationship_service.sync_relationships_for_opportunity(db, a)
            await db.commit()
            assert written == 0

    @pytest.mark.asyncio
    async def test_group_over_the_pairwise_cap_is_skipped(self, db_session_factory, monkeypatch):
        """The real, disclosed scale boundary: a group larger than
        `_MAX_GROUP_SIZE_FOR_PAIRWISE` is skipped, not silently
        materialized in full."""
        monkeypatch.setattr(entity_relationship_service, "_MAX_GROUP_SIZE_FOR_PAIRWISE", 2)
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            ids = [await _make_opportunity(db, source_id=source_id, country=None, title=f"O{i}") for i in range(4)]

            a = await db.get(Opportunity, ids[0])
            written = await entity_relationship_service.sync_relationships_for_opportunity(db, a)
            await db.commit()
            # 4 total incl. `a` itself -- 3 real others, over the cap of 2.
            assert written == 0


class TestRebuildAllRelationships:
    @pytest.mark.asyncio
    async def test_rebuild_from_existing_data_is_idempotent(self, db_session_factory):
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            await _make_opportunity(db, source_id=source_id, country="Egypt", title="A")
            await _make_opportunity(db, source_id=source_id, country="Egypt", title="B")
            await _make_opportunity(db, source_id=source_id, country="Egypt", title="C")

            summary_1 = await entity_relationship_service.rebuild_all_relationships(db)
            await db.commit()
            summary_2 = await entity_relationship_service.rebuild_all_relationships(db)
            await db.commit()

            assert summary_1["real_opportunities_scanned"] == 3
            # 3 opportunities, same org AND same country -> 3*2=6 pairs each
            # direction... same-org and same-country groups are identical
            # here (same 3 ids), so 2 relationship types * 3*2 directed
            # pairs = 12 edges.
            assert summary_1["real_edges_written"] == 12
            assert summary_1["real_groups_skipped_over_cap"] == 0
            assert summary_2["real_edges_written"] == summary_1["real_edges_written"]

            result = await db.execute(select(EntityRelationship))
            assert len(result.scalars().all()) == 12


@pytest.fixture
def client_factory(db_session_factory):
    def _build() -> TestClient:
        async def _override_get_db():
            async with db_session_factory() as session:
                yield session

        app.dependency_overrides[get_db] = _override_get_db
        return TestClient(app)

    yield _build
    app.dependency_overrides.clear()


class TestRelatedOpportunitiesEndpoint:
    @pytest.mark.asyncio
    async def test_unknown_opportunity_is_404(self, db_session_factory, client_factory):
        client = client_factory()
        res = client.get(f"/api/v1/recommendations/{uuid.uuid4()}/related")
        assert res.status_code == 404

    @pytest.mark.asyncio
    async def test_real_edges_returned_with_relationship_type(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            opp_a = await _make_opportunity(db, source_id=source_id, country="Egypt", title="A")
            opp_b = await _make_opportunity(db, source_id=source_id, country="Jordan", title="B")
            a = await db.get(Opportunity, opp_a)
            await entity_relationship_service.sync_relationships_for_opportunity(db, a)
            await db.commit()

        client = client_factory()
        res = client.get(f"/api/v1/recommendations/{opp_a}/related")
        assert res.status_code == 200
        body = res.json()
        assert len(body) == 1
        assert body[0]["id"] == str(opp_b)
        assert body[0]["title"] == "B"
        assert body[0]["relationship_type"] == "same_organization"

    @pytest.mark.asyncio
    async def test_unpublished_related_opportunity_is_excluded(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            opp_a = await _make_opportunity(db, source_id=source_id, country="Egypt", title="A")
            opp_b = await _make_opportunity(db, source_id=source_id, country="Jordan", title="B")
            await db.execute(
                text("UPDATE opportunities SET review_status = 'pending_review' WHERE id = :id"),
                {"id": opp_b.hex},
            )
            await db.commit()
            a = await db.get(Opportunity, opp_a)
            await entity_relationship_service.sync_relationships_for_opportunity(db, a)
            await db.commit()

        client = client_factory()
        res = client.get(f"/api/v1/recommendations/{opp_a}/related")
        assert res.status_code == 200
        assert res.json() == []
