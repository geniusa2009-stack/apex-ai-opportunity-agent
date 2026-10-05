"""Database integration tests on a REAL Postgres + pgvector database.

Skipped unless APEX_TEST_DATABASE_URL is set to an asyncpg URL of a
DISPOSABLE database already migrated to head (`alembic upgrade head`).
The tests TRUNCATE the agent tables and the opportunity/source tables --
never point this at a database whose data you need.

    APEX_TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/apex_test pytest tests/test_agent_postgres_integration.py

What it proves beyond the SQLite unit tests: the migrated schema matches
the models (JSONB, UUID, FKs, unique constraints, check constraints), the
`FOR UPDATE SKIP LOCKED` claim really keeps two workers apart, the
publication idempotency key is enforced by the database itself, and the
pgvector semantic-candidate query runs.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.agent import jobs
from app.agent.approval import approve_draft
from app.agent.dedup import DedupFacts
from app.agent.ingestion import IngestionEvent, ingest_event
from app.agent.llm import LLMClient
from app.agent.llm.fake import FakeLLMProvider
from app.agent.pipeline import AgentDependencies, run_due_jobs, semantic_search_for
from app.agent.publishing import MockPublisher
from app.core.config import settings
from app.models.agent import AgentPostDraft, PipelineJob, Publication, SourceMessage
from app.models.agent_enums import PublicationStatus
from app.models.enums import AdminRole, OpportunityType, SourceType
from app.models.opportunity import Opportunity
from app.models.user import User
from tests.agent_support import SAMPLE_OPPORTUNITY_TEXT, sample_extraction
from tests.test_agent_pipeline_e2e import FakeFetcher

DB_URL = os.environ.get("APEX_TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not DB_URL, reason="APEX_TEST_DATABASE_URL not set (needs a migrated, disposable Postgres)"),
]

_TRUNCATE = (
    "TRUNCATE post_performance, publications, pipeline_job_events, agent_post_drafts, pipeline_jobs, "
    "opportunity_source_messages, opportunity_fingerprints, agent_opportunity_facts, source_messages, "
    "evidence, claims, opportunity_change_history, moderation_log_entries, opportunities, sources CASCADE"
)


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine(DB_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text(_TRUNCATE))
        await conn.execute(text("DELETE FROM users WHERE email LIKE 'agent-it-%'"))
    yield async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


def deps() -> AgentDependencies:
    return AgentDependencies(
        llm=LLMClient(primary=FakeLLMProvider([sample_extraction()]), max_validation_retries=1),
        fetcher=FakeFetcher(),
        publishers=[MockPublisher()],
        today=lambda: dt.date(2026, 10, 5),
    )


async def _ingest(factory, message_id: str) -> uuid.UUID:
    async with factory() as db:
        result = await ingest_event(db, IngestionEvent(source_type=SourceType.WHATSAPP, source_key="g@g.us",
                                                       group_id="g@g.us", external_message_id=message_id,
                                                       text=SAMPLE_OPPORTUNITY_TEXT + f"\n#{message_id}"),
                                    auto_approve_source=True)
        await db.commit()
        return result.job.id


async def test_full_pipeline_on_postgres(factory):
    await _ingest(factory, "pg-1")
    d = deps()
    assert await run_due_jobs(factory, d) == {"waiting_approval": 1}
    async with factory() as db:
        moderator = User(email=f"agent-it-{uuid.uuid4().hex[:6]}@apex.test", full_name="IT", is_active=True,
                         is_superuser=False, admin_role=AdminRole.MODERATOR)
        db.add(moderator)
        await db.flush()
        draft = (await db.execute(select(AgentPostDraft))).scalars().one()
        await approve_draft(db, draft, actor_id=moderator.id, actor_role=AdminRole.MODERATOR)
        await db.commit()
    assert await run_due_jobs(factory, d) == {"completed": 1}
    async with factory() as db:
        publication = (await db.execute(select(Publication))).scalars().one()
        assert publication.status == PublicationStatus.PUBLISHED.value
        message = (await db.execute(select(SourceMessage))).scalars().one()
        assert message.extraction["title"]  # JSONB round trip
        # DB-level idempotency: a second row with the same key is impossible.
        db.add(Publication(idempotency_key=publication.idempotency_key, draft_id=publication.draft_id,
                           opportunity_id=publication.opportunity_id, platform="mock", status="pending"))
        with pytest.raises(IntegrityError):
            await db.flush()


async def test_skip_locked_claims_never_overlap(factory):
    for i in range(6):
        await _ingest(factory, f"pg-claim-{i}")

    async def claim(owner: str) -> set[uuid.UUID]:
        async with factory() as db:
            claimed = await jobs.claim_due_jobs(db, owner=owner, limit=6)
            await asyncio.sleep(0.2)  # hold the row locks while the other worker tries
            await db.commit()
            return {j.id for j in claimed}

    first, second = await asyncio.gather(claim("w1"), claim("w2"))
    assert first.isdisjoint(second)
    assert len(first | second) == 6


async def test_check_constraint_bounds_attempts(factory):
    job_id = await _ingest(factory, "pg-check")
    async with factory() as db:
        job = await db.get(PipelineJob, job_id)
        job.attempts = job.max_attempts + 1
        with pytest.raises(IntegrityError):
            await db.flush()


async def test_semantic_candidate_query_runs_on_pgvector(factory):
    class SameModelEmbedder:
        name = "test"
        model = settings.ai_embedding_model
        dimension = settings.embedding_dimension

        async def embed(self, texts):
            return [[1.0] + [0.0] * (self.dimension - 1) for _ in texts]

    async with factory() as db:
        from app.models.source import Source

        source = Source(name="pg-src", base_url="https://e.org", source_type="website")
        db.add(source)
        await db.flush()
        db.add(Opportunity(title="Similar", target_url="https://e.org/x", source_id=source.id,
                           type=OpportunityType.SCHOLARSHIP, embedding=[1.0] + [0.0] * (settings.embedding_dimension - 1)))
        await db.flush()
        search = semantic_search_for(db, SameModelEmbedder())
        assert search is not None
        found = await search(DedupFacts(title="Similar scholarship"))
        assert len(found) == 1 and list(found.values())[0] == pytest.approx(1.0)
        assert (await db.execute(select(func.count()).select_from(Opportunity))).scalar_one() == 1
        await db.rollback()
