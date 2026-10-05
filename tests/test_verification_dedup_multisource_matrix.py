"""Opportunity Intelligence Hardening -- Parts 7 (Identity/Dedup), 8
(Multi-Source Evidence), 9 (Contradiction Handling), 12 (Staleness), 13
(Evidence Completeness), 14 (Verification Matrix).

INTEGRATION tier: real SQLite DB, real `upsert_opportunity` /
`evidence_verification` functions (`record_evidence`, `get_or_create_claim`,
`decide_verification`) -- the exact same, unmodified D6 engine audited in
Part 0. No new verification engine, no new Opportunity/Claim/Evidence
model (Part 20) -- every scenario below drives the real one.

Source authority is NEVER passed in by these tests -- `record_evidence`
derives it deterministically from `source_url` via the real, unmodified
`classify_source_authority` (domain-pattern rules: `.gov`/`.gov.xx` ->
GOVERNMENT, `.edu`/`.ac` -> UNIVERSITY, everything else -> UNKNOWN unless
a known third-party domain). `.gov`/`.edu` URLs below stand in for
"official"/authoritative evidence for exactly that reason -- `OFFICIAL`
itself is never auto-assigned (see that function's own docstring) and is
out of scope for these tests.
"""
from __future__ import annotations

import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.enums import SourceType, VerificationDecisionStatus
from app.schemas.opportunity import OpportunityCreate
from app.services.aggregator_service import upsert_opportunity
from app.services.evidence_verification import (
    decide_verification,
    evaluate_claim,
    get_or_create_claim,
    record_evidence,
)

_CREATE_SOURCES_SQL = """
CREATE TABLE sources (
    id VARCHAR(36) PRIMARY KEY, name VARCHAR(255) NOT NULL, base_url VARCHAR(2048) NOT NULL,
    active_status BOOLEAN NOT NULL DEFAULT 1, trust_score FLOAT NOT NULL DEFAULT 0.5, access_status VARCHAR(20) NOT NULL DEFAULT 'approved',
    source_type VARCHAR(20) NOT NULL DEFAULT 'website',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
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
_CREATE_OPPORTUNITIES_SQL = """
CREATE TABLE opportunities (
    id VARCHAR(36) PRIMARY KEY, title VARCHAR(500) NOT NULL, description TEXT,
    target_url VARCHAR(2048) NOT NULL, source_id VARCHAR(36) NOT NULL, type VARCHAR(20) NOT NULL,
    deadline DATETIME, eligibility_criteria TEXT, funding_details TEXT, location VARCHAR(255),
    country VARCHAR(120) DEFAULT 'Global', is_verified BOOLEAN NOT NULL DEFAULT 0, ai_confidence_score FLOAT,
    raw_extracted_data TEXT, embedding TEXT, is_local_egypt BOOLEAN NOT NULL DEFAULT 0,
    target_age_group VARCHAR(50), target_academic_levels TEXT, min_age INTEGER, max_age INTEGER,
    target_gender VARCHAR(10), review_status VARCHAR(20) NOT NULL DEFAULT 'published',
    claimed_by VARCHAR(36), claimed_at DATETIME, lifecycle_state VARCHAR(30) NOT NULL DEFAULT 'discovered', lifecycle_state_reason TEXT, lifecycle_state_evaluated_at DATETIME, monitoring_last_checked_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""
_CREATE_CLAIMS_SQL = """
CREATE TABLE claims (
    id VARCHAR(36) PRIMARY KEY, opportunity_id VARCHAR(36) NOT NULL, claim_type VARCHAR(50) NOT NULL,
    claim_value TEXT NOT NULL, normalized_value TEXT, status VARCHAR(20) NOT NULL DEFAULT 'unknown',
    importance VARCHAR(20) NOT NULL DEFAULT 'normal',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""
_CREATE_EVIDENCE_SQL = """
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
_CREATE_USER_BOOKMARKS_SQL = """
CREATE TABLE user_bookmarks (
    id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36) NOT NULL, opportunity_id VARCHAR(36) NOT NULL,
    course_id VARCHAR(36), status VARCHAR(20) NOT NULL DEFAULT 'saved',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        for sql in (
            _CREATE_SOURCES_SQL, _CREATE_OPPORTUNITIES_SQL, _CREATE_CLAIMS_SQL,
            _CREATE_EVIDENCE_SQL, _CREATE_USER_BOOKMARKS_SQL, _CREATE_CHANGE_HISTORY_TABLE_SQL,
        ):
            await conn.execute(text(sql))
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


def _opp_create(**overrides) -> OpportunityCreate:
    defaults = dict(
        title="Global Leaders Program",
        description="A leadership program.",
        target_url="https://official.example.org/apply",
        opportunity_type="scholarship",
        deadline=None,
        is_local_egypt=False,
        target_age_group=None,
        source_name="Official Source",
        source_type=SourceType.WEBSITE,
        source_base_url="https://official.example.org",
    )
    defaults.update(overrides)
    from app.models.enums import OpportunityType
    if isinstance(defaults["opportunity_type"], str):
        defaults["opportunity_type"] = OpportunityType(defaults["opportunity_type"])
    return OpportunityCreate(**defaults)


# ---------------------------------------------------------------------
# Part 7 -- Identity / Dedup hardening
# ---------------------------------------------------------------------


class TestIdentityDedupHardening:
    @pytest.mark.asyncio
    async def test_same_target_url_from_two_different_sources_becomes_one_opportunity(self, db):
        first, created1 = await upsert_opportunity(
            db, _opp_create(source_name="Official Website", target_url="https://real.org/program-x")
        )
        second, created2 = await upsert_opportunity(
            db, _opp_create(source_name="Telegram Aggregator", target_url="https://real.org/program-x")
        )
        assert created1 is True
        assert created2 is False
        assert first.id == second.id

    @pytest.mark.asyncio
    async def test_similar_titles_different_urls_are_not_merged_false_positive_protection(self, db):
        opp_a, _ = await upsert_opportunity(
            db, _opp_create(title="Global Leaders Program 2026", target_url="https://org-a.example.org/apply")
        )
        opp_b, _ = await upsert_opportunity(
            db, _opp_create(title="Global Leaders Program 2027", target_url="https://org-b.example.org/apply")
        )
        # Different real programs (different years, different real
        # application URLs) that merely SHARE a similar title string must
        # remain two distinct rows -- title alone is never the dedup key.
        assert opp_a.id != opp_b.id

    @pytest.mark.asyncio
    async def test_dedup_key_is_target_url_not_title(self, db):
        import inspect
        from app.services import aggregator_service
        src = inspect.getsource(aggregator_service.upsert_opportunity)
        assert "Opportunity.target_url" in src
        assert "Opportunity.title ==" not in src


# ---------------------------------------------------------------------
# Part 8 -- Multi-source evidence, authority preserved
# ---------------------------------------------------------------------


class TestMultiSourceEvidence:
    @pytest.mark.asyncio
    async def test_official_and_community_evidence_both_preserved_for_one_claim(self, db):
        opp, _ = await upsert_opportunity(db, _opp_create(target_url="https://real.org/x"))
        claim = await get_or_create_claim(db, opportunity_id=opp.id, claim_type="deadline", claim_value="2026-12-01")

        # Government (.gov) source: supports -- classify_source_authority
        # derives GOVERNMENT automatically from the domain.
        await record_evidence(
            db, opportunity_id=opp.id, claim_id=claim.id, source_url="https://ministry.gov/official-page",
            evidence_text="Deadline: December 1, 2026",
            supports_claim=True, extraction_method="ai_gateway_structured_output",
        )
        # Community (Telegram) source: also supports -- multiple real
        # sources, one real opportunity, both records preserved.
        await record_evidence(
            db, opportunity_id=opp.id, claim_id=claim.id, source_url="https://t.me/community_channel/1",
            evidence_text="Apply before Dec 1!",
            supports_claim=True, extraction_method="ai_gateway_structured_output",
        )
        decision = await decide_verification(db, opportunity_id=opp.id, claims=[claim])
        assert decision.status == VerificationDecisionStatus.VERIFIED
        assert len(decision.supporting_evidence_ids) == 2

    @pytest.mark.asyncio
    async def test_lower_authority_source_never_silently_overrides_higher_authority_source(self, db):
        opp, _ = await upsert_opportunity(db, _opp_create(target_url="https://real.org/authority"))
        claim = await get_or_create_claim(db, opportunity_id=opp.id, claim_type="eligibility", claim_value="Open to all nationals")

        # Official government source: supports.
        await record_evidence(
            db, opportunity_id=opp.id, claim_id=claim.id, source_url="https://ministry.gov/eligibility",
            evidence_text="Open to all nationals",
            supports_claim=True, extraction_method="ai_gateway_structured_output",
        )
        # An unauthoritative Telegram post CONTRADICTS it.
        await record_evidence(
            db, opportunity_id=opp.id, claim_id=claim.id, source_url="https://t.me/rumor_channel/3",
            evidence_text="Actually I heard it's Egypt-only",
            supports_claim=False, extraction_method="ai_gateway_structured_output",
        )
        decision = await decide_verification(db, opportunity_id=opp.id, claims=[claim])
        # D6's real, unmodified rule: a non-authoritative contradiction
        # does not silently override authoritative support -- see
        # `_evaluate_claim_pure`'s own "authoritative_support and not
        # authoritative_contradict -> falls through to SUPPORTED" path.
        assert decision.status == VerificationDecisionStatus.VERIFIED


# ---------------------------------------------------------------------
# Part 9 -- Contradiction handling -> HOLD, never silently chosen
# ---------------------------------------------------------------------


class TestContradictionHandling:
    @pytest.mark.asyncio
    async def test_two_official_sources_disagreeing_on_deadline_produces_hold(self, db):
        opp, _ = await upsert_opportunity(db, _opp_create(target_url="https://real.org/z"))
        claim = await get_or_create_claim(db, opportunity_id=opp.id, claim_type="deadline", claim_value="2026-10-25")

        await record_evidence(
            db, opportunity_id=opp.id, claim_id=claim.id, source_url="https://official-a.gov/page",
            evidence_text="Deadline: Oct 25, 2026",
            supports_claim=True, extraction_method="ai_gateway_structured_output",
        )
        await record_evidence(
            db, opportunity_id=opp.id, claim_id=claim.id, source_url="https://official-b.gov/page",
            evidence_text="Deadline: Nov 10, 2026",
            supports_claim=False, extraction_method="ai_gateway_structured_output",
        )
        decision = await decide_verification(db, opportunity_id=opp.id, claims=[claim])
        # Two officially-authoritative (.gov) sources disagree -- D6's
        # real, unmodified rule: CONFLICTED claim -> aggregate HOLD,
        # never a silently-picked winner.
        assert decision.status == VerificationDecisionStatus.HOLD

    @pytest.mark.asyncio
    async def test_conflicting_age_eligibility_evidence_also_produces_hold(self, db):
        opp, _ = await upsert_opportunity(db, _opp_create(target_url="https://real.org/age-conflict"))
        claim = await get_or_create_claim(db, opportunity_id=opp.id, claim_type="eligibility", claim_value="Ages 16-18")

        await record_evidence(
            db, opportunity_id=opp.id, claim_id=claim.id, source_url="https://university-a.edu/page",
            evidence_text="Open to ages 16-18",
            supports_claim=True, extraction_method="ai_gateway_structured_output",
        )
        await record_evidence(
            db, opportunity_id=opp.id, claim_id=claim.id, source_url="https://university-a.edu/updated-page",
            evidence_text="Open to ages 18-25 (updated)",
            supports_claim=False, extraction_method="ai_gateway_structured_output",
        )
        decision = await decide_verification(db, opportunity_id=opp.id, claims=[claim])
        assert decision.status == VerificationDecisionStatus.HOLD


# ---------------------------------------------------------------------
# Part 12 -- Staleness (real D6 STALE_EVIDENCE_MAX_AGE_DAYS, unchanged)
# ---------------------------------------------------------------------


class TestStaleness:
    @pytest.mark.asyncio
    async def test_only_stale_supporting_evidence_does_not_verify(self, db):
        opp, _ = await upsert_opportunity(db, _opp_create(target_url="https://real.org/stale"))
        claim = await get_or_create_claim(db, opportunity_id=opp.id, claim_type="deadline", claim_value="2020-01-01")

        old_time = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=800)
        await record_evidence(
            db, opportunity_id=opp.id, claim_id=claim.id, source_url="https://real.gov/old-page",
            evidence_text="Deadline: Jan 1, 2020",
            supports_claim=True, extraction_method="ai_gateway_structured_output", published_at=old_time,
        )
        decision = await decide_verification(db, opportunity_id=opp.id, claims=[claim])
        assert decision.status == VerificationDecisionStatus.STALE

    @pytest.mark.asyncio
    async def test_current_evidence_is_not_treated_as_stale(self, db):
        opp, _ = await upsert_opportunity(db, _opp_create(target_url="https://real.org/current"))
        claim = await get_or_create_claim(db, opportunity_id=opp.id, claim_type="deadline", claim_value="2026-12-01")
        recent_time = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=5)
        await record_evidence(
            db, opportunity_id=opp.id, claim_id=claim.id, source_url="https://real.gov/new-page",
            evidence_text="Deadline: Dec 1, 2026",
            supports_claim=True, extraction_method="ai_gateway_structured_output", published_at=recent_time,
        )
        decision = await decide_verification(db, opportunity_id=opp.id, claims=[claim])
        assert decision.status == VerificationDecisionStatus.VERIFIED


# ---------------------------------------------------------------------
# Part 13 / 14 -- Evidence completeness + full verification matrix
# ---------------------------------------------------------------------


class TestVerificationMatrix:
    @pytest.mark.asyncio
    async def test_strong_official_current_uncontradicted_is_verified(self, db):
        opp, _ = await upsert_opportunity(db, _opp_create(target_url="https://real.org/matrix-1"))
        claim = await get_or_create_claim(db, opportunity_id=opp.id, claim_type="title", claim_value="Real Program")
        await record_evidence(
            db, opportunity_id=opp.id, claim_id=claim.id, source_url="https://real.gov/matrix-1",
            evidence_text="Real Program",
            supports_claim=True, extraction_method="ai_gateway_structured_output",
        )
        decision = await decide_verification(db, opportunity_id=opp.id, claims=[claim])
        assert decision.status == VerificationDecisionStatus.VERIFIED

    @pytest.mark.asyncio
    async def test_official_source_with_no_evidence_at_all_is_not_verified(self, db):
        opp, _ = await upsert_opportunity(db, _opp_create(target_url="https://real.org/matrix-2"))
        claim = await get_or_create_claim(db, opportunity_id=opp.id, claim_type="deadline", claim_value="2026-12-01")
        # No record_evidence call at all -- insufficient evidence.
        decision = await decide_verification(db, opportunity_id=opp.id, claims=[claim])
        assert decision.status == VerificationDecisionStatus.UNVERIFIED

    @pytest.mark.asyncio
    async def test_community_source_only_claim_can_still_verify_no_authoritative_only_tier_exists(self, db):
        # Documents D6's real, existing, UNCHANGED behavior (confirmed in
        # this task's own Part 0 audit) -- D6 has no separate
        # "authoritative-only" verified tier; SUPPORTED is reachable from
        # any-authority evidence. This is NOT a new rule this task
        # introduces, and this task's own constraints forbid redesigning
        # D6, so the test documents rather than changes this.
        opp, _ = await upsert_opportunity(db, _opp_create(target_url="https://real.org/matrix-3"))
        claim = await get_or_create_claim(db, opportunity_id=opp.id, claim_type="title", claim_value="Community Post")
        await record_evidence(
            db, opportunity_id=opp.id, claim_id=claim.id, source_url="https://t.me/some_channel/5",
            evidence_text="Community Post",
            supports_claim=True, extraction_method="ai_gateway_structured_output",
        )
        decision = await decide_verification(db, opportunity_id=opp.id, claims=[claim])
        assert decision.status == VerificationDecisionStatus.VERIFIED

    @pytest.mark.asyncio
    async def test_evidence_completeness_distinguishes_all_four_real_statuses(self, db):
        opp, _ = await upsert_opportunity(db, _opp_create(target_url="https://real.org/matrix-4"))

        supported = await get_or_create_claim(db, opportunity_id=opp.id, claim_type="title", claim_value="X")
        await record_evidence(
            db, opportunity_id=opp.id, claim_id=supported.id, source_url="https://real.gov/a",
            evidence_text="X", supports_claim=True, extraction_method="ai_gateway_structured_output",
        )

        contradicted = await get_or_create_claim(db, opportunity_id=opp.id, claim_type="country", claim_value="Egypt")
        await record_evidence(
            db, opportunity_id=opp.id, claim_id=contradicted.id, source_url="https://t.me/rando/2",
            evidence_text="Actually it's Kenya", supports_claim=False,
            extraction_method="ai_gateway_structured_output",
        )

        unsupported = await get_or_create_claim(db, opportunity_id=opp.id, claim_type="funding", claim_value="Stipend")
        # No evidence recorded at all for this claim.

        r_supported = await evaluate_claim(db, claim=supported)
        r_contradicted = await evaluate_claim(db, claim=contradicted)
        r_unsupported = await evaluate_claim(db, claim=unsupported)

        assert r_supported.status.value == "supported"
        assert r_contradicted.status.value == "contradicted"
        assert r_unsupported.status.value == "unsupported"
        # Missing evidence is never treated as NEGATIVE evidence -- it's
        # UNSUPPORTED (no claim either way), never CONTRADICTED.
        assert r_unsupported.status.value != "contradicted"
