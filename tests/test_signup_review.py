"""Behavioral tests for Review of Suspicious New Signups:
`app/services/signup_review_service.py`'s three real signals (signup
velocity, blacklist-trigger count, moderation-action count) and the new
Owner-only `GET /admin/signups/review` endpoint
(`app/api/v1/endpoints/admin_signups.py`). Also covers the small,
honest `repeat_offender_service.get_offense_events_since` extension
this feature added.

Real HTTP layer via `TestClient` + `get_current_user`/`get_db`
dependency overrides, in-memory SQLite -- same established technique as
`tests/test_high_risk.py`, whose fixture DDL this file mirrors (users/
moderation_log_entries/student_videos), extended with an explicit
`created_at` on `_make_user` since these tests need precise control
over signup timing (velocity clustering, window-boundary cutoffs).
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
from app.db.session import get_db
from app.main import app
from app.models.enums import AdminRole, ModerationAction, ModerationTargetType
from app.models.moderation_log import ModerationLogEntry
from app.models.user import User
from app.services import repeat_offender_service, signup_review_service

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

_CREATE_STUDENT_VIDEOS_TABLE_SQL = """
CREATE TABLE student_videos (
    id VARCHAR(36) PRIMARY KEY,
    student_id VARCHAR(36) NOT NULL,
    title VARCHAR(255) NOT NULL,
    description TEXT,
    hls_master_url VARCHAR(500),
    status VARCHAR(20) NOT NULL DEFAULT 'pending_ai',
    ai_score INTEGER,
    ai_safety_flag BOOLEAN NOT NULL DEFAULT 0,
    ai_feedback TEXT,
    summary_embedding TEXT,
    duration_seconds INTEGER,
    transcript_text TEXT,
    transcript_segments TEXT,
    thumbnail_url VARCHAR(500),
    admin_override BOOLEAN NOT NULL DEFAULT 0,
    admin_notes TEXT,
    claimed_by VARCHAR(36),
    claimed_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_MODERATION_LOG_TABLE_SQL = """
CREATE TABLE moderation_log_entries (
    id VARCHAR(36) PRIMARY KEY,
    actor_id VARCHAR(36),
    actor_role VARCHAR(20),
    action VARCHAR(30) NOT NULL,
    target_type VARCHAR(30) NOT NULL,
    target_id VARCHAR(36) NOT NULL,
    outcome TEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


@pytest_asyncio.fixture
async def db_session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
        pool_reset_on_return=None,  # same real fix applied to test_chat.py/test_security_shield.py
    )
    async with engine.begin() as conn:
        await conn.execute(text(_CREATE_USERS_TABLE_SQL))
        await conn.execute(text(_CREATE_USER_BOOKMARKS_TABLE_SQL))
        await conn.execute(text(_CREATE_STUDENT_VIDEOS_TABLE_SQL))
        await conn.execute(text(_CREATE_MODERATION_LOG_TABLE_SQL))

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


async def _make_user(
    db: AsyncSession,
    *,
    admin_role: AdminRole | None = None,
    full_name: str = "Test User",
    created_at: dt.datetime | None = None,
) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@example.com",
        full_name=full_name,
        admin_role=admin_role,
        created_at=created_at or _now(),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _log(
    db: AsyncSession,
    *,
    action: ModerationAction,
    target_type: ModerationTargetType,
    target_id: uuid.UUID,
    outcome: dict,
    created_at: dt.datetime | None = None,
) -> ModerationLogEntry:
    entry = ModerationLogEntry(
        id=uuid.uuid4(), actor_id=None, actor_role=None, action=action,
        target_type=target_type, target_id=target_id, outcome=outcome,
        created_at=created_at or _now(),
    )
    db.add(entry)
    await db.commit()
    return entry


@pytest.fixture
def client_factory(db_session_factory):
    def _build(user: User) -> TestClient:
        async def _override_get_db():
            async with db_session_factory() as session:
                yield session

        async def _override_get_current_user():
            async with db_session_factory() as session:
                return await session.get(User, user.id)

        app.dependency_overrides[get_db] = _override_get_db
        app.dependency_overrides[get_current_user] = _override_get_current_user
        return TestClient(app)

    yield _build
    app.dependency_overrides.clear()


pytestmark = pytest.mark.asyncio


# =========================================================================
# get_offense_events_since -- the small, honest repeat_offender_service
# extension this feature added.
# =========================================================================
class TestGetOffenseEventsSince:
    async def test_returns_offender_id_and_real_entry_timestamp(self, db_session_factory):
        async with db_session_factory() as db:
            victim = await _make_user(db)
            entry_time = _now() - dt.timedelta(hours=1)
            await _log(
                db, action=ModerationAction.CHAT_AUTO_BAN, target_type=ModerationTargetType.USER,
                target_id=victim.id, outcome={}, created_at=entry_time,
            )
            cutoff = _now() - dt.timedelta(days=1)
            events = await repeat_offender_service.get_offense_events_since(db, cutoff)

        assert len(events) == 1
        offender_id, ts = events[0]
        assert offender_id == victim.id
        # SQLite round-trips a naive datetime even though the column was
        # written with a tz-aware value -- strip tzinfo from the
        # expected side for the comparison (same real quirk this
        # codebase's other SQLite-backed tests already work around).
        ts_naive = ts.replace(tzinfo=None) if ts.tzinfo else ts
        entry_time_naive = entry_time.replace(tzinfo=None)
        assert abs((ts_naive - entry_time_naive).total_seconds()) < 2

    async def test_non_punitive_actions_are_excluded(self, db_session_factory):
        async with db_session_factory() as db:
            user = await _make_user(db)
            await _log(
                db, action=ModerationAction.CHAT_UNBAN, target_type=ModerationTargetType.USER,
                target_id=user.id, outcome={},
            )
            cutoff = _now() - dt.timedelta(days=1)
            events = await repeat_offender_service.get_offense_events_since(db, cutoff)
        assert events == []

    async def test_entries_before_cutoff_are_excluded(self, db_session_factory):
        async with db_session_factory() as db:
            user = await _make_user(db)
            await _log(
                db, action=ModerationAction.CHAT_AUTO_BAN, target_type=ModerationTargetType.USER,
                target_id=user.id, outcome={}, created_at=_now() - dt.timedelta(days=10),
            )
            cutoff = _now() - dt.timedelta(days=1)
            events = await repeat_offender_service.get_offense_events_since(db, cutoff)
        assert events == []


# =========================================================================
# Access tiering -- same Owner-only precedent as flagged-users/high-risk.
# =========================================================================
class TestSignupReviewAccess:
    async def test_plain_student_gets_403(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            student = await _make_user(session)
        client = client_factory(student)
        assert client.get("/api/v1/admin/signups/review").status_code == 403

    async def test_moderator_gets_403(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        assert client.get("/api/v1/admin/signups/review").status_code == 403

    async def test_owner_gets_200(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, admin_role=AdminRole.OWNER)
        client = client_factory(owner)
        assert client.get("/api/v1/admin/signups/review").status_code == 200


class TestSignupReviewEmptyState:
    async def test_no_recent_signups_returns_real_honest_empty_state(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, admin_role=AdminRole.OWNER)
        client = client_factory(owner)
        body = client.get("/api/v1/admin/signups/review").json()
        assert body["items"] == []
        assert body["total"] == 0
        assert body["window_days"] == signup_review_service.RECENT_SIGNUP_LOOKBACK_DAYS
        assert body["velocity_window_minutes"] == signup_review_service.SIGNUP_VELOCITY_WINDOW_MINUTES


class TestSignupReviewScope:
    async def test_a_plain_recent_signup_with_no_signals_has_empty_reasons(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, admin_role=AdminRole.OWNER)
            student = await _make_user(session, full_name="Plain Student")
        client = client_factory(owner)
        body = client.get("/api/v1/admin/signups/review").json()
        assert body["total"] == 1
        item = body["items"][0]
        assert item["user_id"] == str(student.id)
        assert item["cohort_size"] == 0
        assert item["blacklist_trigger_count"] == 0
        assert item["moderation_action_count"] == 0
        assert item["reasons"] == []

    async def test_an_old_signup_outside_the_window_is_excluded(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, admin_role=AdminRole.OWNER)
            await _make_user(
                session, full_name="Old Student",
                created_at=_now() - dt.timedelta(days=signup_review_service.RECENT_SIGNUP_LOOKBACK_DAYS + 5),
            )
        client = client_factory(owner)
        body = client.get("/api/v1/admin/signups/review").json()
        assert body["total"] == 0

    async def test_a_newly_promoted_staff_account_is_excluded(self, db_session_factory, client_factory):
        """Staff accounts (admin_role IS NOT NULL) are internally
        created/promoted, not organic public signups -- including an
        Owner's own bootstrap account or a newly-promoted Moderator here
        would be a real false-positive."""
        async with db_session_factory() as session:
            owner = await _make_user(session, admin_role=AdminRole.OWNER)
            await _make_user(session, full_name="New Moderator", admin_role=AdminRole.MODERATOR)
        client = client_factory(owner)
        body = client.get("/api/v1/admin/signups/review").json()
        assert body["total"] == 0


class TestSignupReviewVelocity:
    async def test_accounts_clustered_within_the_window_report_each_other_as_cohort(self, db_session_factory, client_factory):
        base = _now() - dt.timedelta(hours=1)
        async with db_session_factory() as session:
            owner = await _make_user(session, admin_role=AdminRole.OWNER)
            for i in range(3):
                await _make_user(session, full_name=f"Bulk {i}", created_at=base + dt.timedelta(minutes=i * 2))
        client = client_factory(owner)
        body = client.get("/api/v1/admin/signups/review").json()
        assert body["total"] == 3
        for item in body["items"]:
            assert item["cohort_size"] == 2
            assert any("other account(s) created within" in r for r in item["reasons"])

    async def test_accounts_outside_the_velocity_window_do_not_count_each_other(self, db_session_factory, client_factory):
        base = _now() - dt.timedelta(hours=2)
        async with db_session_factory() as session:
            owner = await _make_user(session, admin_role=AdminRole.OWNER)
            await _make_user(session, full_name="First", created_at=base)
            await _make_user(
                session, full_name="Second",
                created_at=base + dt.timedelta(minutes=signup_review_service.SIGNUP_VELOCITY_WINDOW_MINUTES + 5),
            )
        client = client_factory(owner)
        body = client.get("/api/v1/admin/signups/review").json()
        assert body["total"] == 2
        for item in body["items"]:
            assert item["cohort_size"] == 0
            assert item["reasons"] == []


class TestSignupReviewBlacklistSignal:
    async def test_a_new_account_already_blacklist_triggered_is_flagged(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, admin_role=AdminRole.OWNER)
            signup_time = _now() - dt.timedelta(hours=3)
            student = await _make_user(session, full_name="Spammy Student", created_at=signup_time)
            await _log(
                session, action=ModerationAction.BLACKLIST_TRIGGERED, target_type=ModerationTargetType.USER,
                target_id=student.id, outcome={"content_type": "chat_message"}, created_at=signup_time + dt.timedelta(minutes=5),
            )
        client = client_factory(owner)
        body = client.get("/api/v1/admin/signups/review").json()
        item = next(i for i in body["items"] if i["user_id"] == str(student.id))
        assert item["blacklist_trigger_count"] == 1
        assert any("Blacklisted content rejected" in r for r in item["reasons"])


class TestSignupReviewModerationSignal:
    async def test_a_new_account_already_moderated_is_flagged(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, admin_role=AdminRole.OWNER)
            signup_time = _now() - dt.timedelta(hours=3)
            student = await _make_user(session, full_name="Banned Student", created_at=signup_time)
            await _log(
                session, action=ModerationAction.CHAT_AUTO_BAN, target_type=ModerationTargetType.USER,
                target_id=student.id, outcome={}, created_at=signup_time + dt.timedelta(minutes=10),
            )
        client = client_factory(owner)
        body = client.get("/api/v1/admin/signups/review").json()
        item = next(i for i in body["items"] if i["user_id"] == str(student.id))
        assert item["moderation_action_count"] == 1
        assert any("real moderation action(s)" in r for r in item["reasons"])

    async def test_a_dismissed_report_does_not_count_as_a_moderation_signal(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, admin_role=AdminRole.OWNER)
            student = await _make_user(session, full_name="Reported Student")
            await _log(
                session, action=ModerationAction.CHAT_REPORT_DISMISS, target_type=ModerationTargetType.CHAT_MESSAGE_REPORT,
                target_id=uuid.uuid4(), outcome={},
            )
        client = client_factory(owner)
        body = client.get("/api/v1/admin/signups/review").json()
        item = next(i for i in body["items"] if i["user_id"] == str(student.id))
        assert item["moderation_action_count"] == 0
        assert item["reasons"] == []


class TestSignupReviewSortingAndPagination:
    async def test_flagged_accounts_sort_before_unflagged_ones(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, admin_role=AdminRole.OWNER)
            plain = await _make_user(session, full_name="Plain", created_at=_now() - dt.timedelta(hours=5))
            flagged_time = _now() - dt.timedelta(hours=1)
            flagged = await _make_user(session, full_name="Flagged", created_at=flagged_time)
            await _log(
                session, action=ModerationAction.CHAT_AUTO_BAN, target_type=ModerationTargetType.USER,
                target_id=flagged.id, outcome={}, created_at=flagged_time + dt.timedelta(minutes=1),
            )
        client = client_factory(owner)
        body = client.get("/api/v1/admin/signups/review").json()
        user_ids_in_order = [item["user_id"] for item in body["items"]]
        assert user_ids_in_order.index(str(flagged.id)) < user_ids_in_order.index(str(plain.id))

    async def test_limit_and_offset_are_respected_while_total_reflects_the_full_count(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, admin_role=AdminRole.OWNER)
            for i in range(5):
                await _make_user(session, full_name=f"Student {i}", created_at=_now() - dt.timedelta(minutes=i))
        client = client_factory(owner)
        body = client.get("/api/v1/admin/signups/review?limit=2&offset=0").json()
        assert body["total"] == 5
        assert len(body["items"]) == 2

        body2 = client.get("/api/v1/admin/signups/review?limit=2&offset=2").json()
        assert len(body2["items"]) == 2
        assert {i["user_id"] for i in body["items"]}.isdisjoint({i["user_id"] for i in body2["items"]})
