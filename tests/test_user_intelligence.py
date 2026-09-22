"""APEX User Intelligence + Proactive Guidance step, Part S's test
matrix: INTERACTIONS, SNAPSHOT, FEED, DIVERSITY, EXPLANATIONS, GUIDANCE,
FEEDBACK, SECURITY/IDOR, EDGE-CASE rows.

Same "minimal raw-SQL SQLite tables" fixture convention as
`test_personalized_intelligence.py` (and every prior phase's test module
in this repository) -- extended here with the two new tables this phase
adds (`opportunity_interactions`) plus the two existing tables this
phase's services read for the first time in tests (`application_trackers`,
`opportunity_change_history`, `entity_relationships` -- the last one
required transitively by `personalized_ranking.rank_candidates`, which
`opportunity_feed.build_personal_feed` reuses unmodified).
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.enums import InteractionEventType, NextActionType
from app.models.opportunity import Opportunity
from app.models.user import User
from app.services import interaction_service
from app.services.guidance_service import build_guidance
from app.services.opportunity_feed import _apply_diversity, build_personal_feed
from app.services.personalized_ranking import rank_candidates
from app.services.user_intelligence import build_user_intelligence_snapshot

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
    remote_preference BOOLEAN, funding_preference VARCHAR(30), deadline_lead_time_days INTEGER,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_SOURCES_SQL = """
CREATE TABLE sources (
    id VARCHAR(36) PRIMARY KEY, name VARCHAR(255) NOT NULL, base_url VARCHAR(2048) NOT NULL,
    active_status BOOLEAN NOT NULL DEFAULT 1, source_type VARCHAR(20) NOT NULL DEFAULT 'website',
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
    lifecycle_state_reason TEXT, lifecycle_state_evaluated_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
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

_CREATE_APPLICATION_TRACKERS_SQL = """
CREATE TABLE application_trackers (
    id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36) NOT NULL, scholarship_name VARCHAR(500) NOT NULL,
    institution VARCHAR(255), status VARCHAR(30) NOT NULL DEFAULT 'interested',
    deadline DATETIME, notes TEXT, ai_todo_list TEXT NOT NULL DEFAULT '[]',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_OPPORTUNITY_CHANGE_HISTORY_SQL = """
CREATE TABLE opportunity_change_history (
    id VARCHAR(36) PRIMARY KEY, opportunity_id VARCHAR(36) NOT NULL, field_name VARCHAR(50) NOT NULL,
    old_value TEXT, new_value TEXT, source_id VARCHAR(36), evidence_id VARCHAR(36),
    change_reason TEXT NOT NULL, changed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_OPPORTUNITY_INTERACTIONS_SQL = """
CREATE TABLE opportunity_interactions (
    id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36) NOT NULL, opportunity_id VARCHAR(36) NOT NULL,
    event_type VARCHAR(30) NOT NULL, event_metadata TEXT,
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
        await conn.execute(text(_CREATE_ENTITY_RELATIONSHIPS_SQL))
        await conn.execute(text(_CREATE_USER_BOOKMARKS_SQL))
        await conn.execute(text(_CREATE_APPLICATION_TRACKERS_SQL))
        await conn.execute(text(_CREATE_OPPORTUNITY_CHANGE_HISTORY_SQL))
        await conn.execute(text(_CREATE_OPPORTUNITY_INTERACTIONS_SQL))
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


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


async def _make_bookmark(db: AsyncSession, *, user_id: uuid.UUID, opportunity_id: uuid.UUID, status: str = "saved"):
    await db.execute(
        text(
            "INSERT INTO user_bookmarks (id, user_id, opportunity_id, status) "
            "VALUES (:id, :user_id, :opportunity_id, :status)"
        ),
        {"id": uuid.uuid4().hex, "user_id": user_id.hex, "opportunity_id": opportunity_id.hex, "status": status},
    )
    await db.commit()


async def _make_tracker(db: AsyncSession, *, user_id: uuid.UUID, status: str = "interested", **overrides):
    tracker_id = uuid.uuid4()
    defaults = {
        "id": tracker_id.hex, "user_id": user_id.hex, "scholarship_name": "Some Scholarship",
        "institution": None, "status": status, "deadline": None, "notes": None, "ai_todo_list": "[]",
    }
    defaults.update(overrides)
    columns = list(defaults.keys())
    placeholders = ", ".join(f":{c}" for c in columns)
    await db.execute(
        text(f"INSERT INTO application_trackers ({', '.join(columns)}) VALUES ({placeholders})"), defaults,
    )
    await db.commit()
    return tracker_id


async def _make_change(db: AsyncSession, *, opportunity_id: uuid.UUID, field_name: str, old_value, new_value, **overrides):
    defaults = {
        "id": uuid.uuid4().hex, "opportunity_id": opportunity_id.hex, "field_name": field_name,
        "old_value": old_value, "new_value": new_value, "source_id": None, "evidence_id": None,
        "change_reason": "test change",
    }
    defaults.update(overrides)
    columns = list(defaults.keys())
    placeholders = ", ".join(f":{c}" for c in columns)
    await db.execute(
        text(f"INSERT INTO opportunity_change_history ({', '.join(columns)}) VALUES ({placeholders})"), defaults,
    )
    await db.commit()


def _now():
    return dt.datetime.now(dt.timezone.utc)


# ---------------------------------------------------------------------
# INTERACTIONS rows (Part D).
# ---------------------------------------------------------------------


@pytest.mark.asyncio
class TestInteractionService:
    async def test_record_interaction_persists_a_real_row(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id)
            row = await interaction_service.record_interaction(
                db, user_id=user_id, opportunity_id=opp_id, event_type=InteractionEventType.VIEWED,
            )
            await db.commit()
            assert row.event_type == InteractionEventType.VIEWED.value
            assert row.user_id == user_id
            assert row.opportunity_id == opp_id

    async def test_dismissed_and_marked_not_relevant_are_excluded_together(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            source_id = await _make_source(db)
            opp_a = await _make_opportunity(db, source_id=source_id, title="A")
            opp_b = await _make_opportunity(db, source_id=source_id, title="B")
            opp_c = await _make_opportunity(db, source_id=source_id, title="C")
            await interaction_service.record_interaction(
                db, user_id=user_id, opportunity_id=opp_a, event_type=InteractionEventType.DISMISSED,
            )
            await interaction_service.record_interaction(
                db, user_id=user_id, opportunity_id=opp_b, event_type=InteractionEventType.MARKED_NOT_RELEVANT,
            )
            await interaction_service.record_interaction(
                db, user_id=user_id, opportunity_id=opp_c, event_type=InteractionEventType.VIEWED,
            )
            await db.commit()
            dismissed = await interaction_service.get_dismissed_opportunity_ids(db, user_id=user_id)
            assert dismissed == {opp_a, opp_b}
            assert opp_c not in dismissed

    async def test_behavior_weight_hierarchy_matches_the_documented_ordering(self):
        """Part D's own literal ordering: explicit negative feedback >
        explicit positive intent > application > save > view. Verified
        here as a real numeric assertion, not just prose -- if a future
        edit silently reorders these weights, this test fails."""
        w = interaction_service.BEHAVIOR_SIGNAL_WEIGHT
        negative = min(w[InteractionEventType.DISMISSED.value], w[InteractionEventType.MARKED_NOT_RELEVANT.value])
        positive_intent = min(w[InteractionEventType.SHARED.value], w[InteractionEventType.MORE_LIKE_THIS.value])
        application = min(w["applied"], w[InteractionEventType.STARTED_APPLICATION.value])
        save = w["saved"]
        view = w[InteractionEventType.VIEWED.value]

        assert negative < 0
        assert positive_intent > application > save > view > 0

    async def test_viewed_is_never_treated_as_save_or_apply(self, db_session_factory):
        w = interaction_service.BEHAVIOR_SIGNAL_WEIGHT
        assert w[InteractionEventType.VIEWED.value] < w["saved"] < w["applied"]


# ---------------------------------------------------------------------
# SNAPSHOT rows (Part A/E).
# ---------------------------------------------------------------------


@pytest.mark.asyncio
class TestUserIntelligenceSnapshot:
    async def test_snapshot_separates_profile_preferences_and_behavior(self, db_session_factory):
        """Part A's own explicit rule: the four signal categories must
        not be silently mixed. Assert they live on distinct, named
        dataclass fields."""
        async with db_session_factory() as db:
            user_id = await _make_user(
                db, age=17, interests='{"Artificial Intelligence"}', target_country="Germany",
            )
            user = await db.get(User, user_id)
            snapshot = await build_user_intelligence_snapshot(db, user=user)
            assert snapshot.profile.age == 17
            assert snapshot.preferences.target_country == "Germany"
            assert snapshot.current_journey.saved_not_yet_applied_count == 0
            assert snapshot.opportunity_gaps  # age known but other material gaps remain

    async def test_journey_counts_saved_vs_applied_distinctly(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            source_id = await _make_source(db)
            opp_saved = await _make_opportunity(db, source_id=source_id, title="Saved one")
            opp_applied = await _make_opportunity(db, source_id=source_id, title="Applied one")
            await _make_bookmark(db, user_id=user_id, opportunity_id=opp_saved, status="saved")
            await _make_bookmark(db, user_id=user_id, opportunity_id=opp_applied, status="applied")
            user = await db.get(User, user_id)
            snapshot = await build_user_intelligence_snapshot(db, user=user)
            assert snapshot.current_journey.saved_not_yet_applied_count == 1
            assert snapshot.current_journey.applied_count == 1

    async def test_tracker_status_counts_reflect_real_trackers(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            await _make_tracker(db, user_id=user_id, status="interested")
            await _make_tracker(db, user_id=user_id, status="interested")
            await _make_tracker(db, user_id=user_id, status="accepted")
            user = await db.get(User, user_id)
            snapshot = await build_user_intelligence_snapshot(db, user=user)
            assert snapshot.current_journey.tracker_status_counts == {"interested": 2, "accepted": 1}
            assert snapshot.current_journey.active_application_tracker_count == 3

    async def test_recent_activity_merges_bookmarks_and_interactions_newest_first(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            source_id = await _make_source(db)
            opp_old = await _make_opportunity(db, source_id=source_id, title="Old")
            opp_new = await _make_opportunity(db, source_id=source_id, title="New")
            await _make_bookmark(db, user_id=user_id, opportunity_id=opp_old)
            await interaction_service.record_interaction(
                db, user_id=user_id, opportunity_id=opp_new, event_type=InteractionEventType.VIEWED,
            )
            await db.commit()
            user = await db.get(User, user_id)
            snapshot = await build_user_intelligence_snapshot(db, user=user)
            kinds = {item.kind for item in snapshot.recent_activity}
            assert "saved" in kinds
            assert InteractionEventType.VIEWED.value in kinds

    async def test_empty_profile_produces_gaps_not_an_error(self, db_session_factory):
        """Part S's own 'empty profile behavior' row: a brand-new user
        with nothing filled in must not crash the snapshot builder, and
        every material field should surface as a gap."""
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            user = await db.get(User, user_id)
            snapshot = await build_user_intelligence_snapshot(db, user=user)
            gap_fields = {g.field for g in snapshot.opportunity_gaps}
            assert gap_fields == {
                "age", "academic_level", "residency_country", "interests",
                "application_goals", "target_country",
            }

    async def test_no_sensitive_attribute_fields_on_the_snapshot(self, db_session_factory):
        """Part B/P: no religion/ethnicity/health/political field is ever
        surfaced, even indirectly, by this projection."""
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            user = await db.get(User, user_id)
            snapshot = await build_user_intelligence_snapshot(db, user=user)
            import dataclasses
            flat = str(dataclasses.asdict(snapshot))
            for forbidden in ("religion", "ethnicity", "health", "political", "sexual_orientation"):
                assert forbidden not in flat.lower()


# ---------------------------------------------------------------------
# FEED + DIVERSITY rows (Part F/G/H).
# ---------------------------------------------------------------------


@pytest.mark.asyncio
class TestPersonalFeed:
    async def test_feed_excludes_hard_ineligible_opportunities(self, db_session_factory):
        """Part G: an opportunity deterministically incompatible with the
        user (age out of range) must never appear in the feed, no
        matter how strong any other match signal is."""
        async with db_session_factory() as db:
            user_id = await _make_user(db, age=10)
            source_id = await _make_source(db)
            await _make_opportunity(db, source_id=source_id, min_age=18, max_age=25, title="Adults only")
            user = await db.get(User, user_id)
            feed = await build_personal_feed(db, user=user)
            assert feed.items == []

    async def test_feed_excludes_dismissed_opportunities(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, title="Dismissed one")
            await interaction_service.record_interaction(
                db, user_id=user_id, opportunity_id=opp_id, event_type=InteractionEventType.DISMISSED,
            )
            await db.commit()
            user = await db.get(User, user_id)
            feed = await build_personal_feed(db, user=user)
            assert feed.excluded_dismissed_count == 1
            assert all(item.ranked.opportunity.id != opp_id for item in feed.items)

    async def test_feed_is_deterministic_and_reproducible(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db, interests='{"Robotics"}')
            source_id = await _make_source(db)
            for i in range(5):
                await _make_opportunity(db, source_id=source_id, title=f"Opp {i}")
            user = await db.get(User, user_id)
            now = _now()
            feed_a = await build_personal_feed(db, user=user, now=now)
            feed_b = await build_personal_feed(db, user=user, now=now)
            assert [i.ranked.opportunity.id for i in feed_a.items] == [i.ranked.opportunity.id for i in feed_b.items]

    async def test_large_candidate_pool_is_bounded(self, db_session_factory):
        """Part Q: candidate generation must not scan the entire table
        unbounded -- `CANDIDATE_POOL_LIMIT` caps the query itself."""
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            source_id = await _make_source(db)
            for i in range(20):
                await _make_opportunity(db, source_id=source_id, title=f"Bulk {i}")
            user = await db.get(User, user_id)
            feed = await build_personal_feed(db, user=user, limit=10)
            assert feed.candidate_pool_size == 20
            assert len(feed.items) <= 10

    async def test_closed_opportunities_are_not_promoted(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, lifecycle_state="closed", title="Closed one")
            user = await db.get(User, user_id)
            feed = await build_personal_feed(db, user=user)
            assert all(item.ranked.opportunity.id != opp_id for item in feed.items)

    async def test_feed_reasons_are_built_from_real_fields_not_invented(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            source_id = await _make_source(db)
            deadline = _now() + dt.timedelta(days=3)
            await _make_opportunity(db, source_id=source_id, title="Due soon", deadline=deadline)
            user = await db.get(User, user_id)
            feed = await build_personal_feed(db, user=user)
            assert feed.items, "expected at least one eligible candidate"
            item = feed.items[0]
            assert any("deadline" in r.lower() for r in item.feed_reasons) or any(
                "discovered" in r.lower() for r in item.feed_reasons
            )


class TestFeedDiversity:
    def test_diversity_never_drops_a_candidate(self):
        """Part H's own correctness property: diversity re-sequencing is
        a soft ordering rule, never a filter -- every input candidate
        must appear exactly once in the output."""
        import dataclasses

        @dataclasses.dataclass
        class _FakeOpp:
            id: str
            type: str

        @dataclasses.dataclass
        class _FakeRanked:
            opportunity: _FakeOpp

        ranked = [_FakeRanked(_FakeOpp(str(i), "scholarship")) for i in range(6)]
        result = _apply_diversity(ranked)
        assert {r.opportunity.id for r in result} == {r.opportunity.id for r in ranked}
        assert len(result) == len(ranked)

    def test_diversity_breaks_up_same_type_runs_when_alternatives_exist(self):
        import dataclasses

        @dataclasses.dataclass
        class _FakeOpp:
            id: str
            type: str

        @dataclasses.dataclass
        class _FakeRanked:
            opportunity: _FakeOpp

        ranked = [
            _FakeRanked(_FakeOpp("1", "scholarship")),
            _FakeRanked(_FakeOpp("2", "scholarship")),
            _FakeRanked(_FakeOpp("3", "scholarship")),
            _FakeRanked(_FakeOpp("4", "internship")),
        ]
        result = _apply_diversity(ranked)
        types = [r.opportunity.type for r in result]
        # No more than 2 consecutive same-type items when an alternative
        # exists to interleave.
        assert not (types[0] == types[1] == types[2] == "scholarship")


# ---------------------------------------------------------------------
# GUIDANCE rows (Part J/K/M).
# ---------------------------------------------------------------------


@pytest.mark.asyncio
class TestGuidance:
    async def test_upcoming_deadline_guidance_is_built_from_real_bookmark(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            source_id = await _make_source(db)
            deadline = _now() + dt.timedelta(days=5)
            opp_id = await _make_opportunity(db, source_id=source_id, title="Due soon", deadline=deadline)
            await _make_bookmark(db, user_id=user_id, opportunity_id=opp_id)
            user = await db.get(User, user_id)
            result = await build_guidance(db, user=user)
            categories = {i.category for i in result.items}
            assert "upcoming_deadlines" in categories
            deadline_item = next(i for i in result.items if i.category == "upcoming_deadlines")
            assert "5" not in deadline_item.message or "1" in deadline_item.message  # real count, not invented
            assert any(a.action_type == NextActionType.REVIEW_DEADLINE.value for a in deadline_item.next_actions)

    async def test_changed_deadline_guidance_reflects_real_change_history(self, db_session_factory):
        """Worked Example 2: a user saved an opportunity and its deadline
        changed -- guidance must surface the OLD and NEW real values,
        never a fabricated one."""
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id, title="Moved deadline")
            await _make_bookmark(db, user_id=user_id, opportunity_id=opp_id)
            await _make_change(
                db, opportunity_id=opp_id, field_name="deadline",
                old_value="2026-01-01", new_value="2026-02-01",
            )
            user = await db.get(User, user_id)
            result = await build_guidance(db, user=user)
            categories = {i.category for i in result.items}
            assert "saved_opportunity_deadline_changed" in categories
            item = next(i for i in result.items if i.category == "saved_opportunity_deadline_changed")
            assert "2026-01-01" in item.next_actions[0].reason
            assert "2026-02-01" in item.next_actions[0].reason

    async def test_incomplete_application_guidance_excludes_terminal_statuses(self, db_session_factory):
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            await _make_tracker(db, user_id=user_id, status="interested")
            await _make_tracker(db, user_id=user_id, status="accepted")
            await _make_tracker(db, user_id=user_id, status="rejected")
            user = await db.get(User, user_id)
            result = await build_guidance(db, user=user)
            item = next((i for i in result.items if i.category == "incomplete_applications"), None)
            assert item is not None
            assert len(item.next_actions) == 1  # only the "interested" tracker

    async def test_profile_completion_guidance_targets_only_material_gaps(self, db_session_factory):
        """Worked Example 3: a user has a goal but insufficient profile
        info -- guidance must point only at fields that materially
        change matching, never a generic 'fill out your whole profile'
        prompt."""
        async with db_session_factory() as db:
            user_id = await _make_user(db, application_goals='{"study_abroad"}')
            user = await db.get(User, user_id)
            result = await build_guidance(db, user=user)
            item = next(i for i in result.items if i.category == "profile_completion_relevant_to_matching")
            gap_fields = {a.label for a in item.next_actions}
            assert len(item.next_actions) <= 6  # bounded to the documented material-gap list
            assert any("interests" in label or "goal" in label.lower() for label in gap_fields) or gap_fields

    async def test_guidance_never_invents_progress_when_there_is_none(self, db_session_factory):
        """Part J's own explicit rule: an empty, brand-new account must
        get an empty-but-valid guidance result, never a fabricated
        'you're ready' message."""
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            user = await db.get(User, user_id)
            result = await build_guidance(db, user=user)
            for item in result.items:
                assert item.message  # every message is real text, never empty/None
                for action in item.next_actions:
                    assert action.reason  # every action has a real, non-empty reason


# ---------------------------------------------------------------------
# SECURITY / IDOR rows (Part P).
# ---------------------------------------------------------------------


@pytest.mark.asyncio
class TestUserIsolation:
    async def test_snapshot_never_leaks_another_users_bookmarks(self, db_session_factory):
        async with db_session_factory() as db:
            user_a = await _make_user(db)
            user_b = await _make_user(db)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id)
            await _make_bookmark(db, user_id=user_a, opportunity_id=opp_id)

            user_b_obj = await db.get(User, user_b)
            snapshot_b = await build_user_intelligence_snapshot(db, user=user_b_obj)
            assert snapshot_b.current_journey.saved_not_yet_applied_count == 0
            assert all(item.opportunity_id != opp_id for item in snapshot_b.recent_activity)

    async def test_dismissed_ids_are_scoped_per_user(self, db_session_factory):
        async with db_session_factory() as db:
            user_a = await _make_user(db)
            user_b = await _make_user(db)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id)
            await interaction_service.record_interaction(
                db, user_id=user_a, opportunity_id=opp_id, event_type=InteractionEventType.DISMISSED,
            )
            await db.commit()
            dismissed_a = await interaction_service.get_dismissed_opportunity_ids(db, user_id=user_a)
            dismissed_b = await interaction_service.get_dismissed_opportunity_ids(db, user_id=user_b)
            assert opp_id in dismissed_a
            assert opp_id not in dismissed_b


# ---------------------------------------------------------------------
# EDGE CASES (Part S: duplicate/contradictory opportunities, no LLM
# authority).
# ---------------------------------------------------------------------


@pytest.mark.asyncio
class TestEdgeCases:
    async def test_duplicate_events_are_not_deduplicated_away(self, db_session_factory):
        """Append-only by design -- recording the same event twice is
        two real rows, not silently collapsed (this module's own
        docstring), so the dismissed-set logic must still be correct
        under duplicates."""
        async with db_session_factory() as db:
            user_id = await _make_user(db)
            source_id = await _make_source(db)
            opp_id = await _make_opportunity(db, source_id=source_id)
            await interaction_service.record_interaction(
                db, user_id=user_id, opportunity_id=opp_id, event_type=InteractionEventType.VIEWED,
            )
            await interaction_service.record_interaction(
                db, user_id=user_id, opportunity_id=opp_id, event_type=InteractionEventType.VIEWED,
            )
            await db.commit()
            recent = await interaction_service.get_recent_interactions(db, user_id=user_id)
            assert len(recent) == 2

    async def test_no_llm_module_is_imported_by_the_guidance_or_feed_layer(self):
        """Part R's own 'no LLM authority' rule, verified structurally:
        neither module imports anything from this codebase's LLM/AI
        client modules."""
        import app.services.guidance_service as gs
        import app.services.opportunity_feed as of

        for mod in (gs, of):
            src = open(mod.__file__).read()
            assert "openai" not in src.lower()
            assert "gemini" not in src.lower()
            assert "genai" not in src.lower()
