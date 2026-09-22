"""Behavioral tests for the Gamification: Daily Streaks, Badges, and
Leaderboard step: `app/services/gamification_service.py` (streak
bookkeeping, XP awarding, badge unlocking, leaderboard read) and
`app/api/v1/endpoints/gamification.py` (the real HTTP layer on top of
it).

In-memory SQLite, `StaticPool`. Table creation strategy, per this
codebase's own established discipline (see `tests/test_analytics.py`'s
own docstring on this exact point):

- `users` -- raw SQL (`User.skills` is a Postgres `ARRAY`, which
  SQLite's DDL compiler has no `visit_ARRAY` for).
- `user_bookmarks` -- raw SQL, empty/unused, present ONLY because
  `User.bookmarks` is a `lazy="selectin"` relationship that eagerly
  queries this table the moment any `User` row is fetched/refreshed.
- `gamification_profiles`, `badges`, `user_badges` -- `Base.metadata.
  create_all(tables=[...])` directly. None of the three new models has
  a `JSONB`/`ARRAY` column, so the ORM's own real DDL compiles fine on
  SQLite unmodified -- no raw-SQL table needed for any of them, unlike
  `student_videos`/`lessons` elsewhere in this codebase's test suite.

This step's own two EXPLICIT verification requirements (both under
`TestProcessDailyActivity` below):

1. "Login yesterday + login today = streak 2."
2. "Login 2 days ago + login today = streak reset to 1."

Plus this step's own third explicit requirement ("Ensure the XP
addition correctly unlocks a mock badge if the threshold is met"),
under `TestAddXp` below.
"""

import datetime as dt
import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.badges import Badge, GamificationProfile, UserBadge
from app.models.user import User
from app.services import gamification_service

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------
# Raw-SQL tables (JSONB/ARRAY columns SQLite can't natively DDL-compile)
# ---------------------------------------------------------------------

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
    nationality VARCHAR(120), residency_country VARCHAR(120), languages TEXT, interests TEXT, application_goals TEXT, preferred_opportunity_types TEXT, remote_preference BOOLEAN, funding_preference VARCHAR(30), deadline_lead_time_days INTEGER,
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


_CREATE_NOTIFICATIONS_TABLE_SQL = """
CREATE TABLE notifications (
    id VARCHAR(36) PRIMARY KEY,
    user_id VARCHAR(36) NOT NULL,
    type VARCHAR(50) NOT NULL,
    title VARCHAR(255) NOT NULL,
    message TEXT NOT NULL,
    is_read BOOLEAN NOT NULL DEFAULT 0,
    payload TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


@pytest_asyncio.fixture
async def db_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.execute(text(_CREATE_USERS_TABLE_SQL))
        await conn.execute(text(_CREATE_USER_BOOKMARKS_TABLE_SQL))
        # DISCLOSED, NECESSARY FIX (found running the full suite during the
        # Real Leaderboard System task -- unrelated to that task's own
        # changes, a pre-existing regression from an EARLIER migration):
        # this fixture used to put `Notification.__table__` through
        # `Base.metadata.create_all` directly, on the (once-true) theory
        # that "none of the three new models has a JSONB/ARRAY column"
        # (see this file's own module docstring) covered `Notification`
        # too. It no longer does -- migration 0033 (Actionable
        # Notifications, landed after this fixture was written) added a
        # real `JSONB` column, `Notification.payload`, which SQLite's DDL
        # compiler cannot render at all (`CompileError`). Same fix as
        # `_CREATE_USERS_TABLE_SQL` above already uses for `User.skills`
        # (a Postgres `ARRAY`): a raw-SQL `CREATE TABLE`, `payload` as
        # plain `TEXT` -- ORM-level reads/writes of the `dict | None`
        # Python value still round-trip fine against a TEXT column; it was
        # only DDL compilation of the `JSONB` TYPE itself that failed.
        await conn.execute(text(_CREATE_NOTIFICATIONS_TABLE_SQL))
        await conn.run_sync(
            Base.metadata.create_all,
            # `gamification_service.add_xp`'s badge-unlock loop calls
            # `notification_service.create_notification` for every newly-
            # unlocked badge -- the real `notifications` table it writes
            # into is created via raw SQL just above instead, for the
            # reason disclosed there; only these three still go through
            # the ORM's own real DDL.
            tables=[
                GamificationProfile.__table__,
                Badge.__table__,
                UserBadge.__table__,
            ],
        )

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_session_factory):
    async with db_session_factory() as session:
        yield session


# ---------------------------------------------------------------------
# Fixture-data helpers
# ---------------------------------------------------------------------


async def _make_user(db: AsyncSession, **fields) -> User:
    """`**fields` is a Real Leaderboard System, Level 2 addition -- lets
    the new segmented-leaderboard tests below seed a `target_country`/
    `major`/`academic_level` on a user without needing a second, parallel
    user-factory helper. Every pre-existing call site (no kwargs) behaves
    identically to before."""
    defaults = dict(id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.com", full_name="Test Student")
    defaults.update(fields)
    user = User(**defaults)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_badge(db: AsyncSession, name: str, required_xp: int) -> Badge:
    badge = Badge(id=uuid.uuid4(), name=name, description=f"Awarded at {required_xp} XP.", required_xp=required_xp)
    db.add(badge)
    await db.commit()
    await db.refresh(badge)
    return badge


async def _set_last_activity_date(db: AsyncSession, user_id: uuid.UUID, activity_date: dt.date) -> None:
    """Directly seeds a `GamificationProfile.last_activity_date` in the
    past, bypassing `process_daily_activity` entirely -- necessary so
    these tests can construct "as of yesterday"/"as of 2 days ago"
    starting states without needing to fake the system clock."""
    profile = await gamification_service.get_or_create_profile(db, user_id)
    profile.last_activity_date = activity_date
    profile.current_streak = 1
    profile.highest_streak = max(profile.highest_streak, 1)
    db.add(profile)
    await db.commit()


# ---------------------------------------------------------------------
# Service-layer tests: streak logic
# ---------------------------------------------------------------------


class TestProcessDailyActivity:
    async def test_login_yesterday_then_today_gives_streak_2(self, db_session):
        """This step's own explicit verification requirement #1."""
        user = await _make_user(db_session)
        today = dt.datetime.now(dt.timezone.utc).date()
        yesterday = today - dt.timedelta(days=1)

        await _set_last_activity_date(db_session, user.id, yesterday)

        profile = await gamification_service.process_daily_activity(db_session, user.id)

        assert profile.current_streak == 2
        assert profile.highest_streak == 2
        assert profile.last_activity_date == today

    async def test_login_2_days_ago_then_today_resets_streak_to_1(self, db_session):
        """This step's own explicit verification requirement #2."""
        user = await _make_user(db_session)
        today = dt.datetime.now(dt.timezone.utc).date()
        two_days_ago = today - dt.timedelta(days=2)

        # Seed a starting streak of 5 (as if the user had a 5-day streak
        # that then went stale) -- proves the reset genuinely drops the
        # old streak value rather than merely failing to increment it.
        profile = await gamification_service.get_or_create_profile(db_session, user.id)
        profile.last_activity_date = two_days_ago
        profile.current_streak = 5
        profile.highest_streak = 5
        db_session.add(profile)
        await db_session.commit()

        profile = await gamification_service.process_daily_activity(db_session, user.id)

        assert profile.current_streak == 1
        assert profile.highest_streak == 5, "a stale streak resets current_streak but must not erase the past record"
        assert profile.last_activity_date == today

    async def test_activity_already_recorded_today_is_a_pure_no_op(self, db_session):
        """This step's own literal 'If last_activity_date is today ->
        do nothing' case."""
        user = await _make_user(db_session)

        first = await gamification_service.process_daily_activity(db_session, user.id)
        assert first.current_streak == 1

        second = await gamification_service.process_daily_activity(db_session, user.id)

        assert second.current_streak == 1, "a same-day repeat must not increment the streak further"
        assert second.highest_streak == 1

    async def test_first_ever_activity_starts_streak_at_1(self, db_session):
        user = await _make_user(db_session)

        profile = await gamification_service.process_daily_activity(db_session, user.id)

        assert profile.current_streak == 1
        assert profile.highest_streak == 1

    async def test_streak_never_resets_below_a_true_break(self, db_session):
        """Sanity check on the ladder itself: two consecutive real days
        (not just a single yesterday->today jump) keeps incrementing."""
        user = await _make_user(db_session)
        today = dt.datetime.now(dt.timezone.utc).date()

        await _set_last_activity_date(db_session, user.id, today - dt.timedelta(days=2))
        # 2 days ago -> today: resets to 1 (matches requirement #2).
        p1 = await gamification_service.process_daily_activity(db_session, user.id)
        assert p1.current_streak == 1

        # Manually roll `last_activity_date` back one day (simulating
        # "yesterday", from today's perspective) without touching the
        # streak counters themselves, then run again -- streak should
        # continue to 2, not reset again.
        profile = await gamification_service.get_or_create_profile(db_session, user.id)
        profile.last_activity_date = today - dt.timedelta(days=1)
        db_session.add(profile)
        await db_session.commit()

        p2 = await gamification_service.process_daily_activity(db_session, user.id)
        assert p2.current_streak == 2
        assert p2.highest_streak == 2


# ---------------------------------------------------------------------
# Service-layer tests: XP + badge unlocking
# ---------------------------------------------------------------------


class TestAddXp:
    async def test_xp_below_threshold_unlocks_nothing(self, db_session):
        user = await _make_user(db_session)
        await _make_badge(db_session, "Century Club", required_xp=100)

        profile, unlocked = await gamification_service.add_xp(db_session, user.id, 50, "test")

        assert profile.total_xp == 50
        assert unlocked == []

    async def test_xp_crossing_threshold_unlocks_the_mock_badge(self, db_session):
        """This step's own explicit verification requirement #3: "the XP
        addition correctly unlocks a mock badge if the threshold is
        met."""
        user = await _make_user(db_session)
        badge = await _make_badge(db_session, "Century Club", required_xp=100)

        profile, unlocked = await gamification_service.add_xp(db_session, user.id, 100, "test")

        assert profile.total_xp == 100
        assert [b.id for b in unlocked] == [badge.id]

        # Persisted for real -- confirmed via a fresh, independent read,
        # not just the in-request return value.
        stored_badges = await gamification_service.get_unlocked_badges(db_session, user.id)
        assert [b.id for b in stored_badges] == [badge.id]

    async def test_a_second_award_never_reawards_an_already_unlocked_badge(self, db_session):
        user = await _make_user(db_session)
        await _make_badge(db_session, "Century Club", required_xp=100)

        await gamification_service.add_xp(db_session, user.id, 100, "test")
        profile, unlocked_second_time = await gamification_service.add_xp(db_session, user.id, 10, "test")

        assert profile.total_xp == 110
        assert unlocked_second_time == [], "the badge was already unlocked by the first award"

        stored_badges = await gamification_service.get_unlocked_badges(db_session, user.id)
        assert len(stored_badges) == 1

    async def test_one_award_can_cross_multiple_badge_thresholds_at_once(self, db_session):
        user = await _make_user(db_session)
        bronze = await _make_badge(db_session, "Bronze", required_xp=50)
        silver = await _make_badge(db_session, "Silver", required_xp=100)

        profile, unlocked = await gamification_service.add_xp(db_session, user.id, 150, "test")

        assert profile.total_xp == 150
        assert {b.id for b in unlocked} == {bronze.id, silver.id}

    async def test_zero_or_negative_xp_is_rejected(self, db_session):
        user = await _make_user(db_session)
        with pytest.raises(ValueError):
            await gamification_service.add_xp(db_session, user.id, 0, "test")
        with pytest.raises(ValueError):
            await gamification_service.add_xp(db_session, user.id, -5, "test")


# ---------------------------------------------------------------------
# Service-layer tests: leaderboard
# ---------------------------------------------------------------------


class TestLeaderboard:
    async def test_leaderboard_orders_by_total_xp_descending(self, db_session):
        low = await _make_user(db_session)
        high = await _make_user(db_session)
        mid = await _make_user(db_session)

        await gamification_service.add_xp(db_session, low.id, 10, "test")
        await gamification_service.add_xp(db_session, high.id, 500, "test")
        await gamification_service.add_xp(db_session, mid.id, 200, "test")

        leaderboard = await gamification_service.get_leaderboard(db_session, limit=10)

        assert [p.user_id for p in leaderboard] == [high.id, mid.id, low.id]

    async def test_leaderboard_is_capped_at_the_requested_limit(self, db_session):
        for _ in range(15):
            user = await _make_user(db_session)
            await gamification_service.add_xp(db_session, user.id, 10, "test")

        leaderboard = await gamification_service.get_leaderboard(db_session, limit=10)

        assert len(leaderboard) == 10


# ---------------------------------------------------------------------
# Router-level tests (real HTTP, via TestClient)
# ---------------------------------------------------------------------

# MOVED TO MODULE LEVEL as a Real Leaderboard System, Level 2 change:
# originally defined only inside `TestGamificationRouterHTTP` below (as a
# `self`-scoped fixture), but the new `TestAllTimeRank`/`TestDisplayAlias`/
# `TestSegmentedLeaderboard`/`TestMonthlyPeriod` classes below ALSO need
# real HTTP-level coverage (e.g. confirming `PATCH /gamification/alias`'s
# actual response shape, not just the service function it calls) and a
# pytest fixture defined inside one test class is not visible to another.
# Promoting it to module scope changes nothing about its own behavior or
# `TestGamificationRouterHTTP`'s own usage of it -- module-scoped fixtures
# are visible to every test in the file, class-based or not, same as
# before for that class specifically.
@pytest.fixture
def client_factory(db_session_factory):
    """Same `client_factory` fixture pattern as `tests/test_analytics.py`'s
    own `TestHeartbeatRouterHTTP` -- a fresh `User` row seeded through
    the SAME `db_session_factory` every other fixture/helper in this
    file uses, `get_db`/`get_current_user` dependency-overridden to
    route through it, real `TestClient(app)` HTTP calls against the
    real FastAPI routes."""

    async def _build():
        async with db_session_factory() as setup_session:
            user = await _make_user(setup_session)

        async def _override_get_db():
            async with db_session_factory() as session:
                yield session

        async def _override_get_current_user():
            async with db_session_factory() as session:
                return await session.get(User, user.id)

        app.dependency_overrides[get_db] = _override_get_db
        app.dependency_overrides[get_current_user] = _override_get_current_user

        return TestClient(app), user

    yield _build
    app.dependency_overrides.clear()


class TestGamificationRouterHTTP:
    async def test_my_stats_for_a_brand_new_user_is_all_zeros_not_a_404(self, client_factory):
        client, _user = await client_factory()

        response = client.get("/api/v1/gamification/my-stats")

        assert response.status_code == 200
        body = response.json()
        assert body["total_xp"] == 0
        assert body["current_streak"] == 0
        assert body["badges"] == []

    async def test_daily_login_awards_xp_once_and_is_idempotent_same_day(self, client_factory):
        client, _user = await client_factory()

        first = client.post("/api/v1/gamification/daily-login")
        second = client.post("/api/v1/gamification/daily-login")

        assert first.status_code == 200
        first_body = first.json()
        assert first_body["xp_awarded"] == gamification_service.DAILY_LOGIN_XP
        assert first_body["current_streak"] == 1
        assert first_body["total_xp"] == gamification_service.DAILY_LOGIN_XP

        assert second.status_code == 200
        second_body = second.json()
        assert second_body["xp_awarded"] == 0, "a same-day repeat call must not award XP twice"
        assert second_body["total_xp"] == gamification_service.DAILY_LOGIN_XP
        assert second_body["current_streak"] == 1

    async def test_leaderboard_http_response_shape_and_ranking(self, db_session_factory, client_factory):
        client, user = await client_factory()
        async with db_session_factory() as session:
            other = await _make_user(session)
            await gamification_service.add_xp(session, user.id, 300, "test")
            await gamification_service.add_xp(session, other.id, 50, "test")

        response = client.get("/api/v1/gamification/leaderboard")

        assert response.status_code == 200
        body = response.json()
        assert len(body) == 2
        assert body[0]["rank"] == 1
        assert body[0]["user_id"] == str(user.id)
        assert body[0]["total_xp"] == 300
        assert body[1]["rank"] == 2
        assert body[1]["user_id"] == str(other.id)
        assert body[1]["total_xp"] == 50


# ---------------------------------------------------------------------
# Real Leaderboard System, Level 2: all-time personal rank
# ---------------------------------------------------------------------


class TestAllTimeRank:
    async def test_rank_and_gap_for_a_user_outside_the_top_list(self, db_session):
        low = await _make_user(db_session)
        mid = await _make_user(db_session)
        high = await _make_user(db_session)

        await gamification_service.add_xp(db_session, high.id, 500, "test")
        await gamification_service.add_xp(db_session, mid.id, 200, "test")
        await gamification_service.add_xp(db_session, low.id, 50, "test")

        own = await gamification_service.get_user_alltime_rank(db_session, low.id)

        assert own["total_xp"] == 50
        assert own["rank"] == 3
        assert own["total_participants"] == 3
        assert own["xp_to_next_rank"] == 150, "150 XP separates #3 (50) from #2 (200)"

    async def test_rank_1_has_zero_xp_to_next_rank(self, db_session):
        user = await _make_user(db_session)
        await gamification_service.add_xp(db_session, user.id, 10, "test")

        own = await gamification_service.get_user_alltime_rank(db_session, user.id)

        assert own["rank"] == 1
        assert own["xp_to_next_rank"] == 0

    async def test_brand_new_user_with_zero_xp_still_gets_a_real_rank(self, db_session):
        other = await _make_user(db_session)
        await gamification_service.add_xp(db_session, other.id, 100, "test")
        brand_new = await _make_user(db_session)

        own = await gamification_service.get_user_alltime_rank(db_session, brand_new.id)

        assert own["total_xp"] == 0
        assert own["rank"] == 2
        assert own["total_participants"] == 2

    async def test_http_leaderboard_me_route(self, db_session_factory, client_factory):
        client, user = await client_factory()
        async with db_session_factory() as session:
            other = await _make_user(session)
            await gamification_service.add_xp(session, other.id, 999, "test")
            await gamification_service.add_xp(session, user.id, 10, "test")

        response = client.get("/api/v1/gamification/leaderboard/me")

        assert response.status_code == 200
        body = response.json()
        assert body["total_xp"] == 10
        assert body["rank"] == 2
        assert body["total_participants"] == 2
        assert body["xp_to_next_rank"] == 989


# ---------------------------------------------------------------------
# Real Leaderboard System, Level 2: display alias
# ---------------------------------------------------------------------


class TestDisplayAlias:
    async def test_alias_overrides_full_name_on_the_leaderboard(self, db_session_factory, client_factory):
        client, user = await client_factory()
        async with db_session_factory() as session:
            await gamification_service.add_xp(session, user.id, 100, "test")

        patch_response = client.patch("/api/v1/gamification/alias", json={"alias": "Nightowl42"})
        assert patch_response.status_code == 200
        assert patch_response.json() == {"display_alias": "Nightowl42", "display_name": "Nightowl42"}

        leaderboard_response = client.get("/api/v1/gamification/leaderboard")
        body = leaderboard_response.json()
        assert body[0]["display_name"] == "Nightowl42", "the real full_name must NOT appear once an alias is set"

    async def test_clearing_the_alias_reverts_to_the_real_name_fallback_chain(self, db_session_factory, client_factory):
        client, user = await client_factory()
        async with db_session_factory() as session:
            await gamification_service.add_xp(session, user.id, 100, "test")

        client.patch("/api/v1/gamification/alias", json={"alias": "Temp Alias"})
        clear_response = client.patch("/api/v1/gamification/alias", json={"alias": None})

        assert clear_response.status_code == 200
        assert clear_response.json()["display_alias"] is None
        assert clear_response.json()["display_name"] == "Test Student", "reverts to User.full_name once cleared"

    async def test_whitespace_only_alias_is_normalized_to_cleared_not_stored_literally(
        self, db_session_factory, client_factory
    ):
        client, user = await client_factory()

        response = client.patch("/api/v1/gamification/alias", json={"alias": "   "})

        assert response.status_code == 200
        assert response.json()["display_alias"] is None


# ---------------------------------------------------------------------
# Real Leaderboard System, Level 2: segmented (country/major/academic_level)
# ---------------------------------------------------------------------


class TestSegmentedLeaderboard:
    async def test_country_filter_excludes_non_matching_users(self, db_session):
        egypt_user = await _make_user(db_session, target_country="Egypt")
        canada_user = await _make_user(db_session, target_country="Canada")
        await gamification_service.add_xp(db_session, egypt_user.id, 50, "test")
        await gamification_service.add_xp(db_session, canada_user.id, 500, "test")

        results = await gamification_service.get_leaderboard(db_session, limit=10, country="Egypt")

        assert [p.user_id for p in results] == [egypt_user.id]

    async def test_major_filter_excludes_non_matching_users(self, db_session):
        cs_user = await _make_user(db_session, major="Computer Science")
        bio_user = await _make_user(db_session, major="Biology")
        await gamification_service.add_xp(db_session, cs_user.id, 10, "test")
        await gamification_service.add_xp(db_session, bio_user.id, 10, "test")

        results = await gamification_service.get_leaderboard(db_session, limit=10, major="Computer Science")

        assert [p.user_id for p in results] == [cs_user.id]

    async def test_no_filters_returns_everyone_unfiltered(self, db_session):
        a = await _make_user(db_session, target_country="Egypt")
        b = await _make_user(db_session, target_country="Canada")
        await gamification_service.add_xp(db_session, a.id, 10, "test")
        await gamification_service.add_xp(db_session, b.id, 20, "test")

        results = await gamification_service.get_leaderboard(db_session, limit=10)

        assert len(results) == 2

    async def test_http_leaderboard_route_accepts_country_query_param(self, db_session_factory, client_factory):
        client, user = await client_factory()
        async with db_session_factory() as session:
            db_user = await session.get(User, user.id)
            db_user.target_country = "Egypt"
            session.add(db_user)
            await session.commit()
            other = await _make_user(session, target_country="Canada")
            await gamification_service.add_xp(session, user.id, 10, "test")
            await gamification_service.add_xp(session, other.id, 999, "test")

        response = client.get("/api/v1/gamification/leaderboard?country=Egypt")

        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["user_id"] == str(user.id)


# ---------------------------------------------------------------------
# Real Leaderboard System, Level 2: monthly period
# ---------------------------------------------------------------------


class TestMonthlyPeriod:
    async def test_add_xp_increments_monthly_xp_alongside_total_and_weekly(self, db_session):
        user = await _make_user(db_session)

        profile, _ = await gamification_service.add_xp(db_session, user.id, 40, "test")

        assert profile.total_xp == 40
        assert profile.weekly_xp == 40
        assert profile.monthly_xp == 40

    async def test_monthly_leaderboard_orders_by_monthly_xp_descending(self, db_session):
        low = await _make_user(db_session)
        high = await _make_user(db_session)
        await gamification_service.add_xp(db_session, low.id, 10, "test")
        await gamification_service.add_xp(db_session, high.id, 90, "test")

        results = await gamification_service.get_monthly_leaderboard(db_session, limit=10)

        assert [p.user_id for p in results] == [high.id, low.id]

    async def test_reset_monthly_leaderboard_zeroes_monthly_xp_but_not_total_or_weekly(self, db_session):
        user = await _make_user(db_session)
        await gamification_service.add_xp(db_session, user.id, 75, "test")

        summary = await gamification_service.reset_monthly_leaderboard(db_session)
        assert summary == {"reset": 1}

        profile = await gamification_service.get_or_create_profile(db_session, user.id)
        assert profile.monthly_xp == 0
        assert profile.total_xp == 75, "the monthly reset must never touch the permanent all-time score"
        assert profile.weekly_xp == 75, "the monthly reset must never touch the independent weekly counter"

    async def test_http_months_leaderboard_route_shape(self, db_session_factory, client_factory):
        client, user = await client_factory()
        async with db_session_factory() as session:
            other = await _make_user(session)
            await gamification_service.add_xp(session, user.id, 300, "test")
            await gamification_service.add_xp(session, other.id, 50, "test")

        response = client.get("/api/v1/gamification/months/leaderboard")

        assert response.status_code == 200
        body = response.json()
        assert body["monthly_xp"] == 300
        assert body["rank"] == 1
        assert body["total_participants"] == 2
        assert body["xp_to_next_rank"] == 0
        assert body["top_members"][0]["monthly_xp"] == 300
        assert body["top_members"][1]["monthly_xp"] == 50
