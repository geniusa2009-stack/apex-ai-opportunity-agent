"""APEX Onboarding Production Integration -- INTEGRATION-category test
required by the governing directive's own testing matrix (Section
H / "Confirm that onboarding data actually reaches the appropriate
existing intelligence systems where intended, rather than merely being
stored and ignored").

This file deliberately does NOT re-test the onboarding endpoint's own
request/response contract (see `tests/test_onboarding_integration.py`
for that -- 19/19 passing). It tests exactly one thing, end to end,
through two REAL, pre-existing, unmodified systems:

    POST /users/me/onboarding/complete
        -> commits real columns onto the real `User` row
        -> `build_user_intelligence_snapshot` (app/services/user_intelligence.py),
           called fresh, with NO changes of its own in this phase,
           reflects those committed facts in its `ProfileFacts`,
           `GoalsAndInterests`, and `Preferences` sub-dataclasses.

Reuses the exact same SQLite-ARRAY-write shim and TestClient/dependency-
override fixtures already established in `tests/test_onboarding_integration.py`
(see that file's own module docstring for why the shim exists -- a real
SQLite testing-environment limitation, not a production concern)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import ARRAY as PGArray
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user
from app.db.session import get_db
from app.main import app
from app.models.user import User
from app.services.user_intelligence import build_user_intelligence_snapshot

pytestmark = pytest.mark.asyncio

_CREATE_USERS_TABLE_SQL = """
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
    nationality VARCHAR(120), residency_country VARCHAR(120), languages TEXT, interests TEXT, application_goals TEXT, preferred_opportunity_types TEXT, remote_preference BOOLEAN, funding_preference VARCHAR(30), deadline_lead_time_days INTEGER, date_of_birth DATE, english_level VARCHAR(20), other_nationalities TEXT, onboarding_completed_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


@pytest.fixture(autouse=True)
def _sqlite_array_write_shim():
    """Same scoped, auto-reverting monkeypatch as
    `tests/test_onboarding_integration.py` -- see that file's docstring.
    SQLAlchemy's Postgres `ARRAY` type has no native SQLite bind/result
    processor; this JSON-serializes list columns only when the dialect
    is sqlite, and defers to the real implementation otherwise, so it
    can never change production (Postgres) behavior."""
    import json

    orig_bind = PGArray.bind_processor
    orig_result = PGArray.result_processor

    def patched_bind(self, dialect):
        if dialect.name != "sqlite":
            return orig_bind(self, dialect)

        def process(value):
            if value is None:
                return None
            return json.dumps([v.value if hasattr(v, "value") else v for v in value])

        return process

    def patched_result(self, dialect, coltype):
        if dialect.name != "sqlite":
            return orig_result(self, dialect, coltype)

        item_type = self.item_type

        def process(value):
            if value is None:
                return None
            raw = json.loads(value)
            try:
                item_processor = item_type.result_processor(dialect, coltype)
            except Exception:
                item_processor = None
            if item_processor:
                return [item_processor(v) for v in raw]
            return raw

        return process

    PGArray.bind_processor = patched_bind
    PGArray.result_processor = patched_result
    yield
    PGArray.bind_processor = orig_bind
    PGArray.result_processor = orig_result


_CREATE_USER_BOOKMARKS_SQL = """
CREATE TABLE user_bookmarks (
    id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36) NOT NULL, opportunity_id VARCHAR(36) NOT NULL,
    course_id VARCHAR(36), status VARCHAR(20) NOT NULL DEFAULT 'saved',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

# `load_recent_activity` (called by `build_user_intelligence_snapshot`)
# also queries `OpportunityInteraction` -- `event_metadata` is a
# nullable JSONB column on the real model, declared as plain TEXT here
# (same SQLite-fixture convention this codebase already uses elsewhere
# for JSONB columns in raw-SQL test fixtures); this test never writes
# to it, so no serialization shim is needed for it.
_CREATE_OPPORTUNITY_INTERACTIONS_SQL = """
CREATE TABLE opportunity_interactions (
    id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36) NOT NULL, opportunity_id VARCHAR(36) NOT NULL,
    event_type VARCHAR(30) NOT NULL, event_metadata TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

# Minimal raw-SQL fixture, NOT `Base.metadata.create_all` -- same reason
# `tests/test_onboarding_integration.py` never uses `create_all` either:
# several unrelated tables in this codebase use a native Postgres
# `JSONB` column (e.g. `agent_alerts.matches`), which has no SQLite DDL
# compiler at all (a pre-existing, already-documented limitation -- see
# `tests/test_live_session_reminders.py`/`tests/test_student_videos.py`).
# `build_user_intelligence_snapshot` only ever queries `ApplicationTracker`
# by `user_id`, so only the columns it actually reads need to exist here.
_CREATE_APPLICATION_TRACKERS_SQL = """
CREATE TABLE application_trackers (
    id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36) NOT NULL,
    scholarship_name VARCHAR(500) NOT NULL, institution VARCHAR(255),
    opportunity_id VARCHAR(36), status VARCHAR(30) NOT NULL DEFAULT 'planning',
    deadline DATETIME, notes TEXT, ai_todo_list TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    submitted_at DATETIME
)
"""


@pytest_asyncio.fixture
async def db_session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.execute(text(_CREATE_USERS_TABLE_SQL))
        await conn.execute(text(_CREATE_USER_BOOKMARKS_SQL))
        await conn.execute(text(_CREATE_APPLICATION_TRACKERS_SQL))
        await conn.execute(text(_CREATE_OPPORTUNITY_INTERACTIONS_SQL))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
def client_factory(db_session_factory):
    def _build(current_user):
        async def _get_db_override():
            async with db_session_factory() as session:
                yield session

        app.dependency_overrides[get_db] = _get_db_override
        app.dependency_overrides[get_current_user] = lambda: current_user
        client = TestClient(app)
        return client

    yield _build
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)


async def _make_user(session: AsyncSession, **overrides) -> User:
    defaults = dict(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@example.com",
        full_name="Pending Student",
        is_active=True,
    )
    defaults.update(overrides)
    user = User(**defaults)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


def _valid_payload(**overrides) -> dict:
    payload = {
        "date_of_birth": "2010-05-15",
        "first_name": "Layla",
        "last_name": "Hassan",
        "country": "Egypt",
        "nationalities": ["Egyptian"],
        "stage": "High school",
        "grade": "Grade 11",
        "english_level": "advanced",
        "interests": ["Robotics", "Debate"],
        "looking_for": ["Scholarships & study abroad", "Competitions"],
        "funding": "Fully funded only",
        "where": "Abroad too",
        "goal": "Get into a university",
        "intent": None,
    }
    payload.update(overrides)
    return payload


class TestOnboardingReachesUserIntelligenceSnapshot:
    """The one chain the directive asks this test to prove: onboarding
    submission -> real committed `User` row -> real
    `build_user_intelligence_snapshot` reflects it, unmodified in this
    phase, reused exactly as every other caller (feed/guidance/
    eligibility) already reuses it."""

    async def test_profile_facts_reflect_onboarding_submission(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            user = await _make_user(session)
            user_id = user.id

        client = client_factory(user)
        response = client.post("/api/v1/users/me/onboarding/complete", json=_valid_payload())
        assert response.status_code == 200, response.text

        async with db_session_factory() as session:
            refreshed = await session.get(User, user_id)
            snapshot = await build_user_intelligence_snapshot(session, user=refreshed)

        # ProfileFacts: age is deterministically derived from the
        # submitted date_of_birth (as_of "today"), never AI-determined
        # and never submitted directly by the client.
        assert snapshot.profile.age is not None
        assert snapshot.profile.age >= 13
        assert snapshot.profile.academic_level == "secondary_2"
        assert snapshot.profile.nationality == "Egyptian"
        assert snapshot.profile.full_name == "Layla Hassan"

        # GoalsAndInterests: explicit interests/goals submitted at
        # onboarding are visible to the exact same snapshot that
        # feed/guidance/eligibility already consume.
        assert "Robotics" in snapshot.goals_and_interests.interests
        assert "Debate" in snapshot.goals_and_interests.interests
        assert "study" in snapshot.goals_and_interests.application_goals

        # Preferences: the mapped opportunity-type/remote-preference
        # taxonomy is visible too.
        assert "scholarship" in snapshot.preferences.preferred_opportunity_types
        assert "competition" in snapshot.preferences.preferred_opportunity_types
        assert snapshot.preferences.remote_preference is None  # "Abroad too"

    async def test_resubmission_keeps_snapshot_consistent_not_duplicated(self, db_session_factory, client_factory):
        """Idempotency, traced through to the intelligence layer: a
        second onboarding submission must not create a second user, a
        second row, or a snapshot with duplicated/accumulated facts --
        the snapshot is always built fresh from the one real row."""
        async with db_session_factory() as session:
            user = await _make_user(session)
            user_id = user.id

        client = client_factory(user)
        first = client.post("/api/v1/users/me/onboarding/complete", json=_valid_payload())
        assert first.status_code == 200, first.text
        second = client.post(
            "/api/v1/users/me/onboarding/complete",
            json=_valid_payload(interests=["Robotics", "Debate"]),
        )
        assert second.status_code == 200, second.text

        async with db_session_factory() as session:
            count = (await session.execute(text("SELECT COUNT(*) FROM users"))).scalar_one()
            refreshed = await session.get(User, user_id)
            snapshot = await build_user_intelligence_snapshot(session, user=refreshed)

        assert count == 1
        # No duplicate tags from resubmitting the same explicit interests.
        assert snapshot.goals_and_interests.interests.count("Robotics") == 1
        assert snapshot.goals_and_interests.interests.count("Debate") == 1

    async def test_unmapped_stage_grade_still_reaches_snapshot_as_explicit_gap(self, db_session_factory, client_factory):
        """An honestly-disclosed unmapped taxonomy value (e.g. a
        "Graduate"/"Working" combination with no AcademicLevel
        equivalent) must not silently crash the pipeline or get
        guessed -- `academic_level` stays genuinely None, visible to
        the snapshot as a real gap, not a fabricated value."""
        async with db_session_factory() as session:
            user = await _make_user(session)
            user_id = user.id

        client = client_factory(user)
        response = client.post(
            "/api/v1/users/me/onboarding/complete",
            json=_valid_payload(stage="Graduate", grade="Working"),
        )
        assert response.status_code == 200, response.text
        assert response.json()["taxonomy_notes"]["unmapped_academic_level"] is True

        async with db_session_factory() as session:
            refreshed = await session.get(User, user_id)
            snapshot = await build_user_intelligence_snapshot(session, user=refreshed)

        assert snapshot.profile.academic_level is None
