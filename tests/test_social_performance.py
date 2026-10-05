"""Final Hardening Gate, Section 5: proves the real N+1 found while
inspecting other social/profile paths touched by this phase (beyond
`GET /users/search`, covered by `tests/test_privacy.py::
TestSearchPerformance`) is actually gone -- by counting real SQL
statements executed, not asserting on code shape.

`social_service.get_friend_suggestions` (`GET /social/friends/
suggestions`, reached from the Network page this phase's own Phase 1
work rewrote) used to issue 2 extra queries PER CANDIDATE user on the
platform inside its scoring loop. Same `StaticPool` in-memory SQLite +
`before_cursor_execute` query-counting convention as `test_privacy.py`'s
`TestSearchPerformance` -- this file only counts queries sequentially,
never runs concurrent transactions, so `StaticPool` is the right tool
here (see `tests/test_social_concurrency.py`'s module docstring for why
it is NOT the right tool for genuine concurrency testing).
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.user import User
from app.services import social_service

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

_CREATE_USER_CONNECTIONS_SQL = """
CREATE TABLE user_connections (
    id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36) NOT NULL, friend_id VARCHAR(36) NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'pending',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_USER_ENROLLMENTS_SQL = """
CREATE TABLE user_enrollments (
    user_id VARCHAR(36) NOT NULL, course_id VARCHAR(36) NOT NULL,
    enrolled_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, course_id)
)
"""

_CREATE_GAMIFICATION_PROFILES_SQL = """
CREATE TABLE gamification_profiles (
    user_id VARCHAR(36) PRIMARY KEY,
    total_xp INTEGER NOT NULL DEFAULT 0,
    current_streak INTEGER NOT NULL DEFAULT 0,
    highest_streak INTEGER NOT NULL DEFAULT 0,
    last_activity_date DATE,
    weekly_xp INTEGER NOT NULL DEFAULT 0,
    league VARCHAR(20) NOT NULL DEFAULT 'bronze',
    monthly_xp INTEGER NOT NULL DEFAULT 0,
    display_alias VARCHAR(100)
)
"""


# `User.bookmarks` is `lazy="selectin"` -- every load of a `User` row
# through the ORM (which `get_friend_suggestions`' candidate query does)
# issues an automatic follow-up SELECT against this table.
_CREATE_USER_BOOKMARKS_SQL = """
CREATE TABLE user_bookmarks (
    id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36) NOT NULL, opportunity_id VARCHAR(36),
    course_id VARCHAR(36), status VARCHAR(20) NOT NULL DEFAULT 'saved',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


@pytest_asyncio.fixture
async def engine_and_session():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.execute(text(_CREATE_USERS_SQL))
        await conn.execute(text(_CREATE_USER_CONNECTIONS_SQL))
        await conn.execute(text(_CREATE_USER_ENROLLMENTS_SQL))
        await conn.execute(text(_CREATE_GAMIFICATION_PROFILES_SQL))
        await conn.execute(text(_CREATE_USER_BOOKMARKS_SQL))

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield engine, session
    await engine.dispose()


def _count_queries(engine):
    count = {"n": 0}

    def _on_execute(*args, **kwargs):
        count["n"] += 1

    event.listen(engine.sync_engine, "before_cursor_execute", _on_execute)
    return count


async def _make_candidates(db, n):
    users = [
        User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex}@example.com", full_name=f"Candidate {i}")
        for i in range(n)
    ]
    db.add_all(users)
    await db.commit()
    return users


class TestFriendSuggestionsPerformance:
    @pytest.mark.asyncio
    async def test_suggestions_issues_a_bounded_query_count_regardless_of_candidate_count(
        self, engine_and_session
    ):
        engine, db = engine_and_session
        viewer = User(id=uuid.uuid4(), email="viewer@example.com", full_name="Viewer")
        db.add(viewer)
        await db.commit()

        counter = _count_queries(engine)

        await _make_candidates(db, 5)
        counter["n"] = 0
        await social_service.get_friend_suggestions(db, viewer.id)
        queries_for_5 = counter["n"]

        await _make_candidates(db, 20)
        counter["n"] = 0
        await social_service.get_friend_suggestions(db, viewer.id)
        queries_for_25 = counter["n"]

        # The whole point of the fix: query count does not grow with
        # the number of candidate users on the platform.
        assert queries_for_5 == queries_for_25

    @pytest.mark.asyncio
    async def test_suggestions_scoring_is_still_correct_after_the_bulk_rewrite(self, engine_and_session):
        """Regression check that bulk-fetching enrollments/XP into
        Python dicts didn't change the actual scoring behavior."""
        _, db = engine_and_session
        viewer = User(id=uuid.uuid4(), email="viewer@example.com", full_name="Viewer")
        shared_course = uuid.uuid4()
        buddy = User(id=uuid.uuid4(), email="buddy@example.com", full_name="Buddy")
        stranger = User(id=uuid.uuid4(), email="stranger@example.com", full_name="Stranger")
        db.add_all([viewer, buddy, stranger])
        await db.commit()

        # Give the viewer a high XP band so a bare "same low XP band as
        # nobody has a profile" coincidence can't accidentally score
        # `stranger` a match -- isolates the assertion to the course-
        # overlap signal only, which is what this test is actually about.
        await db.execute(
            text("INSERT INTO gamification_profiles (user_id, total_xp) VALUES (:u, :xp)"),
            {"u": viewer.id.hex, "xp": 2000},
        )
        await db.execute(
            text("INSERT INTO user_enrollments (user_id, course_id) VALUES (:u, :c)"),
            {"u": viewer.id.hex, "c": shared_course.hex},
        )
        await db.execute(
            text("INSERT INTO user_enrollments (user_id, course_id) VALUES (:u, :c)"),
            {"u": buddy.id.hex, "c": shared_course.hex},
        )
        await db.commit()

        results = await social_service.get_friend_suggestions(db, viewer.id)
        result_ids = {r["user_id"] for r in results}
        assert buddy.id in result_ids  # shared course -> a real, scored suggestion
        assert stranger.id not in result_ids  # no shared course, no XP band match -> score 0, excluded

        buddy_result = next(r for r in results if r["user_id"] == buddy.id)
        assert buddy_result["shared_courses"] == 1
        assert buddy_result["match_score"] == 3  # +3 for the one shared course