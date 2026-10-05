"""Shared fixtures/helpers for the AI Opportunity Agent tests.

The legacy `opportunities`/`sources`/`claims`/`evidence` tables use
Postgres-only column types (ARRAY, Vector, JSONB), so -- exactly like the
rest of this test suite -- they are created from raw SQLite DDL here. The
agent's own tables use portable types and are created from the real ORM
metadata, so these tests exercise the real models.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.agent import (
    AgentOpportunityFacts,
    AgentPostDraft,
    OpportunityFingerprint,
    OpportunitySourceMessage,
    PipelineJob,
    PipelineJobEvent,
    PostPerformance,
    Publication,
    SourceMessage,
)
from app.models.source import Source

CREATE_SOURCES_SQL = """
CREATE TABLE sources (
    id VARCHAR(36) PRIMARY KEY, name VARCHAR(255) NOT NULL UNIQUE, base_url VARCHAR(2048) NOT NULL,
    active_status BOOLEAN NOT NULL DEFAULT 1,
    trust_score FLOAT NOT NULL DEFAULT 0.5, access_status VARCHAR(20) NOT NULL DEFAULT 'approved',
    source_type VARCHAR(20) NOT NULL DEFAULT 'website',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

CREATE_OPPORTUNITIES_SQL = """
CREATE TABLE opportunities (
    id VARCHAR(36) PRIMARY KEY, title VARCHAR(500) NOT NULL, description TEXT,
    target_url VARCHAR(2048) NOT NULL, source_id VARCHAR(36) NOT NULL, type VARCHAR(20) NOT NULL,
    deadline DATETIME, eligibility_criteria TEXT, funding_details TEXT, location VARCHAR(255),
    country VARCHAR(120) DEFAULT 'Global', is_verified BOOLEAN NOT NULL DEFAULT 0, ai_confidence_score FLOAT,
    raw_extracted_data TEXT, embedding TEXT, is_local_egypt BOOLEAN NOT NULL DEFAULT 0,
    target_age_group VARCHAR(50), target_academic_levels TEXT, min_age INTEGER, max_age INTEGER,
    target_gender VARCHAR(10), review_status VARCHAR(20) NOT NULL DEFAULT 'pending_review',
    claimed_by VARCHAR(36), claimed_at DATETIME, lifecycle_state VARCHAR(30) NOT NULL DEFAULT 'discovered',
    lifecycle_state_reason TEXT, lifecycle_state_evaluated_at DATETIME, monitoring_last_checked_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

CREATE_CLAIMS_SQL = """
CREATE TABLE claims (
    id VARCHAR(36) PRIMARY KEY, opportunity_id VARCHAR(36) NOT NULL, claim_type VARCHAR(50) NOT NULL,
    claim_value TEXT NOT NULL, normalized_value TEXT, status VARCHAR(20) NOT NULL DEFAULT 'unknown',
    importance VARCHAR(20) NOT NULL DEFAULT 'normal',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

CREATE_EVIDENCE_SQL = """
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

CREATE_USER_BOOKMARKS_SQL = """
CREATE TABLE user_bookmarks (
    id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36) NOT NULL, opportunity_id VARCHAR(36) NOT NULL,
    course_id VARCHAR(36), status VARCHAR(20) NOT NULL DEFAULT 'saved',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

CREATE_MODERATION_LOG_SQL = """
CREATE TABLE moderation_log_entries (
    id VARCHAR(36) PRIMARY KEY, actor_id VARCHAR(36), actor_role VARCHAR(20), action VARCHAR(30) NOT NULL,
    target_type VARCHAR(30) NOT NULL, target_id VARCHAR(36) NOT NULL, outcome TEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

CREATE_CHANGE_HISTORY_SQL = """
CREATE TABLE opportunity_change_history (
    id VARCHAR(36) PRIMARY KEY, opportunity_id VARCHAR(36) NOT NULL, field_name VARCHAR(50) NOT NULL,
    old_value TEXT, new_value TEXT, source_id VARCHAR(36), evidence_id VARCHAR(36),
    change_reason TEXT NOT NULL, changed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    severity VARCHAR(20)
)
"""

AGENT_TABLES = [
    SourceMessage.__table__,
    OpportunitySourceMessage.__table__,
    OpportunityFingerprint.__table__,
    AgentOpportunityFacts.__table__,
    PipelineJob.__table__,
    PipelineJobEvent.__table__,
    AgentPostDraft.__table__,
    Publication.__table__,
    PostPerformance.__table__,
]


async def make_engine_and_session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
        # Standing fix for StaticPool + aiosqlite reset-on-checkin races
        # (see tests/test_chat.py history).
        pool_reset_on_return=None,
    )
    async with engine.begin() as conn:
        for ddl in (CREATE_SOURCES_SQL, CREATE_OPPORTUNITIES_SQL, CREATE_CLAIMS_SQL, CREATE_EVIDENCE_SQL,
                    CREATE_USER_BOOKMARKS_SQL, CREATE_MODERATION_LOG_SQL, CREATE_CHANGE_HISTORY_SQL):
            await conn.execute(text(ddl))
        for table in AGENT_TABLES:
            await conn.run_sync(table.create)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    return engine, factory


async def make_source(
    db: AsyncSession,
    *,
    name: str | None = None,
    source_type: str = "whatsapp",
    trust_score: float = 0.5,
    access_status: str = "approved",
    base_url: str = "https://chat.whatsapp.com",
) -> Source:
    source = Source(
        name=name or f"source-{uuid.uuid4().hex[:8]}",
        base_url=base_url,
        source_type=source_type,
        trust_score=trust_score,
        access_status=access_status,
        active_status=True,
    )
    db.add(source)
    await db.flush()
    return source


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


SAMPLE_OPPORTUNITY_TEXT = (
    "🔥 *منحة DAAD للماجستير في ألمانيا 2027* 🔥\n"
    "الجهة: DAAD - German Academic Exchange Service\n"
    "منحة ممولة بالكامل تشمل المصروفات الدراسية وراتب شهري 934 يورو.\n"
    "الشروط: خريجي البكالوريوس بخبرة عملية سنتين على الأقل.\n"
    "آخر موعد للتقديم: 15 أكتوبر 2026\n"
    "رابط التقديم: https://www.daad.de/en/study-and-research-in-germany/scholarships/?utm_source=whatsapp&utm_medium=group"
)


def sample_extraction(**overrides: Any) -> dict[str, Any]:
    """A valid `ExtractedOpportunity` payload (as an LLM would return it)."""
    data: dict[str, Any] = {
        "is_opportunity": True,
        "title": "DAAD Master's Scholarship in Germany 2027",
        "organization": "DAAD - German Academic Exchange Service",
        "opportunity_type": "scholarship",
        "description": "Fully funded master's scholarship covering tuition and a monthly stipend.",
        "eligibility": "Bachelor's graduates with at least two years of work experience.",
        "age_min": None,
        "age_max": None,
        "country_requirement": None,
        "location": "Germany",
        "is_online": False,
        "funding": "Fully funded: tuition plus a monthly stipend of 934 EUR.",
        "deadline": "2026-10-15",
        "start_date": None,
        "end_date": None,
        "application_url": "https://www.daad.de/en/study-and-research-in-germany/scholarships/",
        "official_url": None,
        "required_documents": [],
        "contact": None,
        "language": "ar",
        "confidence": 0.9,
        "missing_fields": [],
    }
    data.update(overrides)
    return data
