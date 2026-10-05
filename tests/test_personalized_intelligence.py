"""Personalized Opportunity Intelligence + Opportunity Passport step,
Part Q's test matrix: PROFILE, ELIGIBILITY, HARD FILTER, MATCHING,
EXPLANATIONS, PASSPORT, SECURITY rows.

Same "minimal raw-SQL SQLite tables" fixture convention as every prior
phase's test module in this repository -- extended here with the 9 new
`users.*` personalization columns this phase adds.
"""

from __future__ import annotations

import datetime as dt
import inspect
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.enums import EligibilityStatus, MatchLevel, OpportunityType
from app.models.opportunity import Opportunity
from app.models.user import User
from app.schemas.user import UserProfileUpdate
from app.services import evidence_verification as ev
from app.services.eligibility_engine import evaluate_eligibility
from app.services.opportunity_passport import build_passport
from app.services.personalized_ranking import (
    DEFAULT_EXPLORATION_RATIO,
    compute_match_signals,
    personalization_score,
    rank_candidates,
)

_CREATE_USERS_SQL = """
CREATE TABLE users (
    id VARCHAR(36) PRIMARY KEY,
    email VARCHAR(255) UNIQUE,
    hashed_password VARCHAR(255),
    full_name VARCHAR(255),
    is_active BOOLEAN NOT NULL DEFAULT 1,
    is_superuser BOOLEAN NOT NULL DEFAULT 0,
    phone VARCHAR(50),
    degree_level VARCHAR(100),
    major VARCHAR(255),
    gpa FLOAT,
    graduation_year INTEGER,
    skills TEXT,
    target_country VARCHAR(120),
    age_group VARCHAR(50),
    age INTEGER,
    academic_level VARCHAR(20),
    gender VARCHAR(10),
    bio TEXT,
    search_vector TEXT,
    role VARCHAR(20) NOT NULL DEFAULT 'student',
    admin_role VARCHAR(20),
    role_granted_at DATETIME,
    nationality VARCHAR(120), residency_country VARCHAR(120), languages TEXT,
    interests TEXT, application_goals TEXT, preferred_opportunity_types TEXT,
    remote_preference BOOLEAN, funding_preference VARCHAR(30), deadline_lead_time_days INTEGER, date_of_birth DATE, english_level VARCHAR(20), other_nationalities TEXT, onboarding_completed_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_SOURCES_SQL = """
CREATE TABLE sources (
    id VARCHAR(36) PRIMARY KEY, name VARCHAR(255) NOT NULL, base_url VARCHAR(2048) NOT NULL,
    active_status BOOLEAN NOT NULL DEFAULT 1, trust_score FLOAT NOT NULL DEFAULT 0.5, access_status VARCHAR(20) NOT NULL DEFAULT 'approved',
    source_type VARCHAR(20) NOT NULL DEFAULT 'website',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
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
    claimed_by VARCHAR(36), claimed_at DATETIME,
    lifecycle_state VARCHAR(30) NOT NULL DEFAULT 'discovered',
    lifecycle_state_reason TEXT, lifecycle_state_evaluated_at DATETIME, monitoring_last_checked_at DATETIME,
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

_CREATE_CONTRADICTIONS_SQL = """
CREATE TABLE contradictions (
    id VARCHAR(36) PRIMARY KEY, opportunity_id VARCHAR(36) NOT NULL, claim_id VARCHAR(36) NOT NULL,
    supporting_evidence_id VARCHAR(36), contradicting_evidence_id VARCHAR(36) NOT NULL,
    contradiction_type VARCHAR(30) NOT NULL, severity VARCHAR(20) NOT NULL,
    source_authority VARCHAR(20) NOT NULL DEFAULT 'unknown',
    detected_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, explanation TEXT NOT NULL,
    contradiction_metadata TEXT
)
"""

_CREATE_CHANGE_HISTORY_SQL = """
CREATE TABLE opportunity_change_history (
    id VARCHAR(36) PRIMARY KEY, opportunity_id VARCHAR(36) NOT NULL, field_name VARCHAR(50) NOT NULL,
    old_value TEXT, new_value TEXT, source_id VARCHAR(36), evidence_id VARCHAR(36),
    change_reason TEXT NOT NULL, changed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    severity VARCHAR(20)
)
"""

_CREATE_ENTITY_RELATIONSHIPS_SQL = """
CREATE TABLE entity_relationships (
    id VARCHAR(36) PRIMARY KEY, opportunity_id VARCHAR(36) NOT NULL, related_opportunity_id VARCHAR(36) NOT NULL,
    relationship_type VARCHAR(30) NOT NULL, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
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
async def db_session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.execute(text(_CREATE_USERS_SQL))
        await conn.execute(text(_CREATE_SOURCES_SQL))
        await conn.execute(text(_CREATE_OPPORTUNITIES_SQL))
        await conn.execute(text(_CREATE_CLAIMS_SQL))
        await conn.execute(text(_CREATE_EVIDENCE_SQL))
        await conn.execute(text(_CREATE_CONTRADICTIONS_SQL))
        await conn.execute(text(_CREATE_CHANGE_HISTORY_SQL))
        await conn.execute(text(_CREATE_ENTITY_RELATIONSHIPS_SQL))
        await conn.execute(text(_CREATE_USER_BOOKMARKS_SQL))
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


def _arr(values):
    """Postgres-array-literal-style string, matching the ARRAY-column
    string convention already proven in `test_opportunity_state_machine.py`
    (`target_academic_levels` as `"{a,b}"`) for plain-TEXT SQLite
    fixture columns."""
    if not values:
        return None
    return "{" + ",".join(values) + "}"


async def _make_user(db: AsyncSession, **overrides) -> uuid.UUID:
    user_id = uuid.uuid4()
    defaults = {
        "id": user_id.hex, "email": f"{user_id.hex}@example.com", "full_name": "Test User",
        "age": None, "academic_level": None, "gender": None, "target_country": None,
        "nationality": None, "residency_country": None, "languages": None, "interests": None,
        "application_goals": None, "preferred_opportunity_types": None, "remote_preference": None,
        "funding_preference": None, "deadline_lead_time_days": None,
    }
    defaults.update(overrides)
    columns = list(defaults.keys())
    placeholders = ", ".join(f":{c}" for c in columns)
    await db.execute(
        text(f"INSERT INTO users ({', '.join(columns)}) VALUES ({placeholders})"), defaults,
    )
    await db.commit()
    return user_id


async def _make_source(db: AsyncSession) -> uuid.UUID:
    source_id = uuid.uuid4()
    await db.execute(
        text("INSERT INTO sources (id, name, base_url) VALUES (:id, 'Test Source', 'https://example.com')"),
        {"id": source_id.hex},
    )
    await db.commit()
    return source_id


async def _make_opportunity(db: AsyncSession, *, source_id: uuid.UUID, **overrides) -> uuid.UUID:
    opp_id = uuid.uuid4()
    defaults = {
        "id": opp_id.hex, "title": "Some Opportunity", "description": "A great opportunity in research.",
        "target_url": "https://example.com/apply", "source_id": source_id.hex, "type": "scholarship",
        "deadline": None, "funding_details": None, "country": "Global",
        "min_age": None, "max_age": None, "target_academic_levels": None, "target_gender": None,
        "lifecycle_state": "open",
    }
    defaults.update(overrides)
    columns = list(defaults.keys())
    placeholders = ", ".join(f":{c}" for c in columns)
    await db.execute(
        text(f"INSERT INTO opportunities ({', '.join(columns)}) VALUES ({placeholders})"), defaults,
    )
    await db.commit()
    return opp_id


# ---------------------------------------------------------------------
# PROFILE rows (Part Q / Part A).
# ---------------------------------------------------------------------


class TestUserOpportunityProfile:
    def test_profile_update_accepts_all_new_fields(self):
        payload = UserProfileUpdate(
            nationality="Egyptian", residency_country="Egypt", languages=["English", "Arabic"],
            interests=["Artificial Intelligence", "Robotics"], application_goals=["study", "research"],
            preferred_opportunity_types=["scholarship", "research_funding"], remote_preference=True,
            funding_preference="fully_funded", deadline_lead_time_days=30,
        )
        dumped = payload.model_dump(exclude_unset=True)
        assert dumped["nationality"] == "Egyptian"
        assert dumped["residency_country"] == "Egypt"
        assert dumped["languages"] == ["English", "Arabic"]
        assert dumped["application_goals"] == ["study", "research"]

    def test_profile_update_unset_fields_are_excluded_not_nulled(self):
        """`exclude_unset=True` -- the existing generic `PATCH /users/me`
        setattr loop's own established 'None means leave unchanged'
        semantics (Part A's own precedent, reused unmodified)."""
        payload = UserProfileUpdate(nationality="Egyptian")
        dumped = payload.model_dump(exclude_unset=True)
        assert "residency_country" not in dumped
        assert "interests" not in dumped

    def test_no_sensitive_attribute_inference_fields_exist_on_the_schema(self):
        """Part A's own explicit prohibition: no inferred nationality,
        age, gender, religion, ethnicity, health, or political-view
        field was added by this phase -- only the 9 explicitly-designed
        profile fields."""
        new_fields = {
            "nationality", "residency_country", "languages", "interests", "application_goals",
            "preferred_opportunity_types", "remote_preference", "funding_preference", "deadline_lead_time_days",
        }
        assert new_fields.issubset(set(UserProfileUpdate.model_fields.keys()))
        assert "religion" not in UserProfileUpdate.model_fields
        assert "ethnicity" not in UserProfileUpdate.model_fields
        assert "health_condition" not in UserProfileUpdate.model_fields


# ---------------------------------------------------------------------
# ELIGIBILITY rows (Part B/C/D).
# ---------------------------------------------------------------------


@pytest.mark.asyncio
class TestEligibilityEngine:
    async def test_age_eligible(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db, age=20)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, min_age=18, max_age=25)
            user, opportunity = await db.get(User, user_id), await db.get(Opportunity, opp_id)
            result = evaluate_eligibility(user, opportunity)
            assert result.status == EligibilityStatus.ELIGIBLE

    async def test_age_ineligible(self, db_session_factory):
        """Part B's own literal example: age 18-25 required, user is 16
        -> INELIGIBLE."""
        async with db_session_factory() as db:
            user_id = await _make_user(db, age=16)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, min_age=18, max_age=25)
            user, opportunity = await db.get(User, user_id), await db.get(Opportunity, opp_id)
            result = evaluate_eligibility(user, opportunity)
            assert result.status == EligibilityStatus.INELIGIBLE
            assert any("16" in r.explanation and "18" in r.explanation for r in result.failed_rules)

    async def test_age_unknown(self, db_session_factory):
        """Part B's own literal example: opportunity states an age
        requirement, user's age is unstated -> UNKNOWN, never
        collapsed into INELIGIBLE."""
        async with db_session_factory() as db:
            user_id = await _make_user(db, age=None)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, min_age=18, max_age=25)
            user, opportunity = await db.get(User, user_id), await db.get(Opportunity, opp_id)
            result = evaluate_eligibility(user, opportunity)
            assert result.status == EligibilityStatus.UNKNOWN
            assert result.missing_information == ["age"]

    async def test_academic_level_eligible(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db, academic_level="university_year_1")
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(
                db, source_id=source_id, target_academic_levels="{university_year_1,university_year_2}",
            )
            user, opportunity = await db.get(User, user_id), await db.get(Opportunity, opp_id)
            result = evaluate_eligibility(user, opportunity)
            assert result.status == EligibilityStatus.ELIGIBLE

    async def test_academic_level_ineligible(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db, academic_level="secondary_1")
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(
                db, source_id=source_id, target_academic_levels="{university_year_1,university_year_2}",
            )
            user, opportunity = await db.get(User, user_id), await db.get(Opportunity, opp_id)
            result = evaluate_eligibility(user, opportunity)
            assert result.status == EligibilityStatus.INELIGIBLE

    async def test_academic_level_unknown(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db, academic_level=None)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(
                db, source_id=source_id, target_academic_levels="{university_year_1}",
            )
            user, opportunity = await db.get(User, user_id), await db.get(Opportunity, opp_id)
            result = evaluate_eligibility(user, opportunity)
            assert result.status == EligibilityStatus.UNKNOWN

    async def test_residency_match(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db, residency_country="Egypt")
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, country="Egypt")
            user, opportunity = await db.get(User, user_id), await db.get(Opportunity, opp_id)
            result = evaluate_eligibility(user, opportunity)
            assert result.status == EligibilityStatus.ELIGIBLE

    async def test_residency_mismatch(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db, residency_country="Jordan")
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, country="Egypt")
            user, opportunity = await db.get(User, user_id), await db.get(Opportunity, opp_id)
            result = evaluate_eligibility(user, opportunity)
            assert result.status == EligibilityStatus.INELIGIBLE

    async def test_conditional_eligibility(self, db_session_factory):
        """Part B's 'CONDITIONAL' status: one matched dimension, one
        unknown dimension, zero failures."""
        async with db_session_factory() as db:
            user_id = await _make_user(db, age=20, academic_level=None)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(
                db, source_id=source_id, min_age=18, max_age=25, target_academic_levels="{university_year_1}",
            )
            user, opportunity = await db.get(User, user_id), await db.get(Opportunity, opp_id)
            result = evaluate_eligibility(user, opportunity)
            assert result.status == EligibilityStatus.CONDITIONAL

    async def test_no_requirements_is_eligible(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id)
            user, opportunity = await db.get(User, user_id), await db.get(Opportunity, opp_id)
            result = evaluate_eligibility(user, opportunity)
            assert result.status == EligibilityStatus.ELIGIBLE
            assert result.matched_rules == result.failed_rules == result.unknown_rules == []

    async def test_do_not_fabricate_zero_rules_when_zero_requirements(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db, age=20, academic_level="university_year_1")
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id)
            user, opportunity = await db.get(User, user_id), await db.get(Opportunity, opp_id)
            result = evaluate_eligibility(user, opportunity)
            assert len(result.matched_rules) + len(result.failed_rules) + len(result.unknown_rules) == 0


# ---------------------------------------------------------------------
# MATCHING rows (Part G/S).
# ---------------------------------------------------------------------


@pytest.mark.asyncio
class TestMatchSignals:
    async def test_interest_match(self, db_session_factory):
        """Same `ARRAY(String)` SQLite round-trip caveat as `test_goal_match`
        above -- constructed directly in Python for the same reason."""
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            user = User(id=uuid.uuid4(), interests=["Artificial Intelligence"])
            opportunity = Opportunity(
                id=uuid.uuid4(), title="Artificial Intelligence Fellowship", description="Research in AI.",
                target_url="https://example.com/apply", source_id=source_id, type=OpportunityType.SCHOLARSHIP,
                country="Global",
            )
            signals = await compute_match_signals(db, user=user, opportunity=opportunity, now=dt.datetime.now(dt.timezone.utc))
            assert signals.interest_match in (MatchLevel.MEDIUM, MatchLevel.STRONG)

    async def test_goal_match(self, db_session_factory):
        """`application_goals` is an `ARRAY(String)` column -- confirmed via a
        direct diagnostic to round-trip as a list of individual characters
        through this module's raw-SQL SQLite fixture (a SQLite testing
        artifact only; real Postgres round-trips it correctly, and
        `target_academic_levels`/`preferred_opportunity_types` -- ARRAY of a
        native enum -- are confirmed unaffected). `compute_match_signals`
        never queries `db` for `goal_match`, so this test constructs the
        `User`/`Opportunity` objects directly in Python instead."""
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            user = User(id=uuid.uuid4(), application_goals=["research"])
            opportunity = Opportunity(
                id=uuid.uuid4(), title="Research Opportunity", description="",
                target_url="https://example.com/apply", source_id=source_id, type=OpportunityType.RESEARCH_FUNDING,
                country="Global",
            )
            signals = await compute_match_signals(db, user=user, opportunity=opportunity, now=dt.datetime.now(dt.timezone.utc))
            assert signals.goal_match == MatchLevel.HIGH

    async def test_type_match(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db, preferred_opportunity_types=_arr(["scholarship"]))
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, type="scholarship")
            user, opportunity = await db.get(User, user_id), await db.get(Opportunity, opp_id)
            signals = await compute_match_signals(db, user=user, opportunity=opportunity, now=dt.datetime.now(dt.timezone.utc))
            assert signals.type_match == MatchLevel.STRONG

    async def test_geography_match(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db, target_country="Germany")
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, country="Germany")
            user, opportunity = await db.get(User, user_id), await db.get(Opportunity, opp_id)
            signals = await compute_match_signals(db, user=user, opportunity=opportunity, now=dt.datetime.now(dt.timezone.utc))
            assert signals.geography_match == MatchLevel.STRONG

    async def test_funding_match(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db, funding_preference="fully_funded")
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, funding_details="This is a fully funded scholarship.")
            user, opportunity = await db.get(User, user_id), await db.get(Opportunity, opp_id)
            signals = await compute_match_signals(db, user=user, opportunity=opportunity, now=dt.datetime.now(dt.timezone.utc))
            assert signals.funding_match == MatchLevel.HIGH

    async def test_deadline_match(self, db_session_factory):
        async with db_session_factory() as db:
            now = dt.datetime.now(dt.timezone.utc)
            user_id = await _make_user(db, deadline_lead_time_days=10)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, deadline=now + dt.timedelta(days=20))
            user, opportunity = await db.get(User, user_id), await db.get(Opportunity, opp_id)
            signals = await compute_match_signals(db, user=user, opportunity=opportunity, now=now)
            assert signals.deadline_match == MatchLevel.HIGH

    async def test_graph_relevance_signal_via_rank_candidates(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            source_id = await _make_source(db)
            bookmarked_id = await _make_opportunity(db, source_id=source_id, title="Bookmarked Opp")
            candidate_id = await _make_opportunity(db, source_id=source_id, title="Related Opp")
            await db.execute(
                text(
                    "INSERT INTO entity_relationships (id, opportunity_id, related_opportunity_id, relationship_type) "
                    "VALUES (:id, :opp, :related, 'same_organization')"
                ),
                {"id": uuid.uuid4().hex, "opp": candidate_id.hex, "related": bookmarked_id.hex},
            )
            await db.commit()
            user = await db.get(User, user_id)
            candidate = await db.get(Opportunity, candidate_id)
            ranked = await rank_candidates(
                db, user=user, opportunities=[candidate], bookmarked_opportunity_ids={bookmarked_id}, now=dt.datetime.now(dt.timezone.utc),
            )
            assert len(ranked) == 1
            assert ranked[0].signals.graph_relevance == MatchLevel.HIGH

    async def test_deterministic_ranking_is_reproducible(self, db_session_factory):
        """Part S: same inputs -> same `personalization_score`, twice."""
        async with db_session_factory() as db:
            now = dt.datetime.now(dt.timezone.utc)
            user_id = await _make_user(
                db, interests=_arr(["Robotics"]), application_goals=_arr(["research"]), target_country="Germany",
            )
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(
                db, source_id=source_id, title="Robotics Research Fellowship", type="research_funding", country="Germany",
            )
            user, opportunity = await db.get(User, user_id), await db.get(Opportunity, opp_id)
            signals_1 = await compute_match_signals(db, user=user, opportunity=opportunity, now=now)
            signals_2 = await compute_match_signals(db, user=user, opportunity=opportunity, now=now)
            assert personalization_score(signals_1) == personalization_score(signals_2)


# ---------------------------------------------------------------------
# HARD FILTER rows (Part C/H).
# ---------------------------------------------------------------------


@pytest.mark.asyncio
class TestHardFilterSafety:
    async def test_ineligible_never_ranked(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db, age=16)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, min_age=18, max_age=25)
            user = await db.get(User, user_id)
            opportunity = await db.get(Opportunity, opp_id)
            ranked = await rank_candidates(
                db, user=user, opportunities=[opportunity], bookmarked_opportunity_ids=set(), now=dt.datetime.now(dt.timezone.utc),
            )
            assert ranked == []

    async def test_closed_opportunity_excluded(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, lifecycle_state="closed")
            user = await db.get(User, user_id)
            opportunity = await db.get(Opportunity, opp_id)
            ranked = await rank_candidates(
                db, user=user, opportunities=[opportunity], bookmarked_opportunity_ids=set(), now=dt.datetime.now(dt.timezone.utc),
            )
            assert ranked == []

    async def test_hold_opportunity_excluded(self, db_session_factory):
        """F-HOLDFILTER regression (P0-A): before this fix, `rank_candidates`
        excluded CLOSED only -- a HOLD opportunity (D6's evidence pipeline
        found the available evidence insufficient/outdated) passed straight
        through to ranking/display exactly like a verified-OPEN one."""
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, lifecycle_state="hold")
            user = await db.get(User, user_id)
            opportunity = await db.get(Opportunity, opp_id)
            ranked = await rank_candidates(
                db, user=user, opportunities=[opportunity], bookmarked_opportunity_ids=set(), now=dt.datetime.now(dt.timezone.utc),
            )
            assert ranked == []

    async def test_conflict_opportunity_excluded(self, db_session_factory):
        """F-HOLDFILTER regression (P0-A): a CONFLICT opportunity (D6 found
        unresolved contradictory authoritative evidence) must be excluded
        exactly like CLOSED/HOLD -- there is no surface where showing one
        is safe."""
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, lifecycle_state="conflict")
            user = await db.get(User, user_id)
            opportunity = await db.get(Opportunity, opp_id)
            ranked = await rank_candidates(
                db, user=user, opportunities=[opportunity], bookmarked_opportunity_ids=set(), now=dt.datetime.now(dt.timezone.utc),
            )
            assert ranked == []

    async def test_open_opportunity_still_survives(self, db_session_factory):
        """Sanity check alongside the two regressions above: the fix must
        exclude CLOSED/HOLD/CONFLICT specifically, not silently broaden
        into excluding everything -- a real OPEN opportunity must still
        survive `rank_candidates` unchanged."""
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, lifecycle_state="open")
            user = await db.get(User, user_id)
            opportunity = await db.get(Opportunity, opp_id)
            ranked = await rank_candidates(
                db, user=user, opportunities=[opportunity], bookmarked_opportunity_ids=set(), now=dt.datetime.now(dt.timezone.utc),
            )
            assert len(ranked) == 1
            assert ranked[0].opportunity.id == opp_id

    async def test_unknown_and_eligible_both_survive_distinctly(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db, age=None)
            source_id = await _make_source(db)
            unknown_opp = await _make_opportunity(db, source_id=source_id, min_age=18, max_age=25, title="Age Gated")
            eligible_opp = await _make_opportunity(db, source_id=source_id, title="Open To All")
            user = await db.get(User, user_id)
            opportunities = [await db.get(Opportunity, unknown_opp), await db.get(Opportunity, eligible_opp)]
            ranked = await rank_candidates(
                db, user=user, opportunities=opportunities, bookmarked_opportunity_ids=set(), now=dt.datetime.now(dt.timezone.utc),
            )
            statuses = {r.opportunity.id: r.eligibility.status for r in ranked}
            assert statuses[unknown_opp] == EligibilityStatus.UNKNOWN
            assert statuses[eligible_opp] == EligibilityStatus.ELIGIBLE

    async def test_high_interest_cannot_promote_an_ineligible_opportunity(self, db_session_factory):
        """Part C's own literal example: age 18-25 required, user is 16,
        high interest/goal match on the opportunity -- must still be
        excluded entirely, never ranked around."""
        async with db_session_factory() as db:
            user_id = await _make_user(
                db, age=16, interests=_arr(["Robotics"]), application_goals=_arr(["research"]),
            )
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(
                db, source_id=source_id, min_age=18, max_age=25, type="research_funding",
                title="Robotics Research Fellowship", description="Robotics research.",
            )
            user = await db.get(User, user_id)
            opportunity = await db.get(Opportunity, opp_id)
            ranked = await rank_candidates(
                db, user=user, opportunities=[opportunity], bookmarked_opportunity_ids=set(), now=dt.datetime.now(dt.timezone.utc),
            )
            assert ranked == []


# ---------------------------------------------------------------------
# EXPLANATIONS rows (Part I/J/M).
# ---------------------------------------------------------------------


@pytest.mark.asyncio
class TestExplanations:
    async def test_matched_high_signal_produces_a_reason(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db, preferred_opportunity_types=_arr(["scholarship"]))
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, type="scholarship")
            user, opportunity = await db.get(User, user_id), await db.get(Opportunity, opp_id)
            ranked = await rank_candidates(
                db, user=user, opportunities=[opportunity], bookmarked_opportunity_ids=set(), now=dt.datetime.now(dt.timezone.utc),
            )
            assert any("preferred opportunity type" in r for r in ranked[0].reasons)

    async def test_missing_information_shown_for_unknown(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db, age=None)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, min_age=18, max_age=25)
            user, opportunity = await db.get(User, user_id), await db.get(Opportunity, opp_id)
            ranked = await rank_candidates(
                db, user=user, opportunities=[opportunity], bookmarked_opportunity_ids=set(), now=dt.datetime.now(dt.timezone.utc),
            )
            assert len(ranked) == 1
            assert any("age" in m for m in ranked[0].missing_information)


def test_no_sensitive_attribute_ever_appears_in_an_explanation_string():
    from app.services.personalized_ranking import build_explanation

    source = inspect.getsource(build_explanation)
    for forbidden in ("gender", "nationality", "residency", "user.age"):
        assert forbidden not in source


# ---------------------------------------------------------------------
# PRIVACY BOUNDARY (Part M), enforced structurally.
# ---------------------------------------------------------------------


def test_compute_match_signals_never_reads_sensitive_user_fields():
    import app.services.personalized_ranking as module

    source = inspect.getsource(module)
    for forbidden in ("user.gender", "user.age", "user.nationality", "user.residency_country"):
        assert forbidden not in source


# ---------------------------------------------------------------------
# EXPLORATION RATIO rows (Part L).
# ---------------------------------------------------------------------


@pytest.mark.asyncio
class TestExplorationRatio:
    async def test_exploration_ratio_half_yields_half_exploration_slots(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            source_id = await _make_source(db)
            opportunities = []
            for i in range(10):
                opp_id = await _make_opportunity(db, source_id=source_id, title=f"Opportunity {i}")
                opportunities.append(await db.get(Opportunity, opp_id))
            user = await db.get(User, user_id)
            ranked = await rank_candidates(
                db, user=user, opportunities=opportunities, bookmarked_opportunity_ids=set(),
                now=dt.datetime.now(dt.timezone.utc), exploration_ratio=0.5, limit=10,
            )
            assert sum(1 for r in ranked if r.is_exploration) == 5

    async def test_exploration_ratio_zero_yields_no_exploration(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            source_id = await _make_source(db)
            opportunities = []
            for i in range(10):
                opp_id = await _make_opportunity(db, source_id=source_id, title=f"Opportunity {i}")
                opportunities.append(await db.get(Opportunity, opp_id))
            user = await db.get(User, user_id)
            ranked = await rank_candidates(
                db, user=user, opportunities=opportunities, bookmarked_opportunity_ids=set(),
                now=dt.datetime.now(dt.timezone.utc), exploration_ratio=0.0, limit=10,
            )
            assert sum(1 for r in ranked if r.is_exploration) == 0




# ---------------------------------------------------------------------
# PASSPORT rows (Part E/F/O).
# ---------------------------------------------------------------------


@pytest.mark.asyncio
class TestOpportunityPassport:
    async def test_identity_section(self, db_session_factory):
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, title="A Real Opportunity")
            opportunity = await db.get(Opportunity, opp_id)
            passport = await build_passport(db, opportunity=opportunity, requesting_user=None)
            assert passport.identity.title == "A Real Opportunity"
            assert passport.identity.id == opp_id

    async def test_eligibility_none_when_anonymous(self, db_session_factory):
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id)
            opportunity = await db.get(Opportunity, opp_id)
            passport = await build_passport(db, opportunity=opportunity, requesting_user=None)
            assert passport.eligibility is None

    async def test_eligibility_present_for_authenticated_user(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db, age=20)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, min_age=18, max_age=25)
            user, opportunity = await db.get(User, user_id), await db.get(Opportunity, opp_id)
            passport = await build_passport(db, opportunity=opportunity, requesting_user=user)
            assert passport.eligibility is not None
            assert passport.eligibility.status == "eligible"

    async def test_temporal_section(self, db_session_factory):
        async with db_session_factory() as db:
            now = dt.datetime.now(dt.timezone.utc)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(
                db, source_id=source_id, deadline=now + dt.timedelta(days=30), lifecycle_state="open",
            )
            opportunity = await db.get(Opportunity, opp_id)
            passport = await build_passport(db, opportunity=opportunity, requesting_user=None, now=now)
            assert passport.temporal.lifecycle_state == "open"
            assert passport.temporal.deadline is not None

    async def test_trust_section_zero_evidence(self, db_session_factory):
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id)
            opportunity = await db.get(Opportunity, opp_id)
            passport = await build_passport(db, opportunity=opportunity, requesting_user=None)
            assert passport.trust.verification_status == "NOT_VERIFIED"
            assert passport.trust.evidence_count == 0

    async def test_provenance_section_with_real_evidence(self, db_session_factory):
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id)
            claim = await ev.get_or_create_claim(db, opportunity_id=opp_id, claim_type="deadline", claim_value="Confirmed.")
            await ev.record_evidence(
                db, opportunity_id=opp_id, claim_id=claim.id, evidence_text="Confirmed.",
                source_id=source_id, source_url="https://example.com/apply", supports_claim=True,
            )
            await db.commit()
            opportunity = await db.get(Opportunity, opp_id)
            passport = await build_passport(db, opportunity=opportunity, requesting_user=None)
            assert passport.provenance.claim_count == 1
            assert len(passport.provenance.evidence_ids) == 1
            assert passport.trust.verification_status == "VERIFIED"

    async def test_contradiction_count_section(self, db_session_factory):
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id)
            claim = await ev.get_or_create_claim(db, opportunity_id=opp_id, claim_type="deadline", claim_value="X")
            ev1 = await ev.record_evidence(
                db, opportunity_id=opp_id, claim_id=claim.id, evidence_text="Says open.",
                source_id=source_id, source_url="https://example.com/a", supports_claim=True,
            )
            ev2 = await ev.record_evidence(
                db, opportunity_id=opp_id, claim_id=claim.id, evidence_text="Says closed.",
                source_id=source_id, source_url="https://example.com/b", supports_claim=False,
            )
            await db.commit()
            await db.execute(
                text(
                    "INSERT INTO contradictions (id, opportunity_id, claim_id, supporting_evidence_id, "
                    "contradicting_evidence_id, contradiction_type, severity, explanation) VALUES "
                    "(:id, :opp, :claim, :sup, :con, 'direct_conflict', 'high', 'Conflicting evidence.')"
                ),
                {
                    "id": uuid.uuid4().hex, "opp": opp_id.hex, "claim": claim.id.hex,
                    "sup": ev1.id.hex, "con": ev2.id.hex,
                },
            )
            await db.commit()
            opportunity = await db.get(Opportunity, opp_id)
            passport = await build_passport(db, opportunity=opportunity, requesting_user=None)
            assert passport.trust.contradiction_count == 1

    async def test_recent_changes_section(self, db_session_factory):
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id)
            await db.execute(
                text(
                    "INSERT INTO opportunity_change_history (id, opportunity_id, field_name, old_value, new_value, change_reason) "
                    "VALUES (:id, :opp, 'deadline', '2026-10-25', '2026-11-10', 'Extended by source.')"
                ),
                {"id": uuid.uuid4().hex, "opp": opp_id.hex},
            )
            await db.commit()
            opportunity = await db.get(Opportunity, opp_id)
            passport = await build_passport(db, opportunity=opportunity, requesting_user=None)
            assert len(passport.temporal.recent_changes) == 1
            assert passport.temporal.recent_changes[0]["old_value"] == "2026-10-25"


# ---------------------------------------------------------------------
# SECURITY / NO-LLM-AUTHORITY rows (Part R).
# ---------------------------------------------------------------------


class TestNoLLMAuthority:
    def test_evaluate_eligibility_signature_is_structured_only(self):
        params = set(inspect.signature(evaluate_eligibility).parameters)
        assert params == {"user", "opportunity"}

    def test_eligibility_engine_module_has_no_llm_reference(self):
        import app.services.eligibility_engine as module

        source = open(module.__file__, encoding="utf-8").read().lower()
        for forbidden in ("genai", "gemini", "openai", "\"llm\"", "'llm'"):
            assert forbidden not in source


def test_default_exploration_ratio_is_configurable_constant():
    assert DEFAULT_EXPLORATION_RATIO == 0.2
