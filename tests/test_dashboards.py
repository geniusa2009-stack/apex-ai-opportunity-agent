"""Behavioral tests for the Student Dashboard & Admin Control Panel step:

- `app/api/v1/endpoints/dashboards.py` (`GET /dashboards/student`,
  `GET /dashboards/admin`) -- the two aggregated hub endpoints.
- The new/extended service-layer functions this step added to back them:
  `chat_security.list_banned_users`/`unban_user`, `notification_service.
  list_notifications(unread_only=True)`, `gamification_service.
  get_next_badge_threshold`.
- The two new admin action endpoints this step added: `POST
  /admin/videos/{video_id}/reject-and-purge-media` and `DELETE
  /chat/admin/bans/{user_id}`.

In-memory SQLite, `StaticPool`. Table set, per this codebase's own
established discipline (see `tests/test_admin_moderation.py`/`tests/
test_aggregator.py`'s own docstrings for the same reasoning applied to
each table below):

- `users` -- raw SQL, PLUS this step's own new `age_group` column (the
  one real schema addition this step makes -- see app/models/user.py).
- `user_bookmarks` -- raw SQL, empty/unused (`User.bookmarks`/
  `Opportunity.bookmarks` are both `lazy="selectin"`).
- `student_videos` -- raw SQL, same full column list (incl.
  `admin_override`/`admin_notes`/`thumbnail_url`) as `tests/
  test_admin_moderation.py`'s own `_CREATE_STUDENT_VIDEOS_TABLE_SQL`.
- `sources`/`opportunities` -- raw SQL, same shape as `tests/
  test_aggregator.py`'s own tables (JSONB/Vector columns have no SQLite
  DDL compiler support).
- `gamification_profiles`/`badges`/`user_badges`/`notifications` --
  `Base.metadata.create_all(tables=[...])` directly (none of the four
  have a JSONB/ARRAY/Vector column, so the ORM's own real DDL compiles
  fine on SQLite unmodified).

NO REAL REDIS reachable in this sandbox -- `chat_security`'s module
docstring already establishes this; `_FakeRedis` here implements exactly
the four commands this step's own two new functions need
(`scan_iter`/`ttl`/`delete`, plus `set` for seeding a ban), same pattern
as `tests/test_chat.py`'s own fixture, extended with the two additional
commands `list_banned_users`/`unban_user` require that the chat test's
own fake didn't need.
"""

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
from app.models.enums import StudentVideoStatus
from app.models.dangerous_action_confirmation import DangerousActionConfirmation
from app.models.student_videos import StudentVideo
from app.models.user import User
from app.services import chat_security, gamification_service, notification_service

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------
# Raw-SQL tables
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
    nationality VARCHAR(120), residency_country VARCHAR(120), languages TEXT, interests TEXT, application_goals TEXT, preferred_opportunity_types TEXT, remote_preference BOOLEAN, funding_preference VARCHAR(30), deadline_lead_time_days INTEGER, date_of_birth DATE, english_level VARCHAR(20), other_nationalities TEXT, onboarding_completed_at DATETIME,
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
    claimed_at DATETIME, lifecycle_state VARCHAR(30) NOT NULL DEFAULT 'discovered', lifecycle_state_reason TEXT, lifecycle_state_evaluated_at DATETIME, monitoring_last_checked_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

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
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    review_status VARCHAR(20) NOT NULL DEFAULT 'published',
    claimed_by VARCHAR(36),
    claimed_at DATETIME, lifecycle_state VARCHAR(30) NOT NULL DEFAULT 'discovered', lifecycle_state_reason TEXT, lifecycle_state_evaluated_at DATETIME, monitoring_last_checked_at DATETIME
)
"""

# Real Leaderboard System task, disclosed fix -- see the fixture's own
# comment on why this moved from `Base.metadata.create_all` to raw SQL.
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

# NEW as of the Automatic Moderation Activity Log step: this file's own
# `TestRejectAndPurgeMedia`/`TestAdminUnban` tests exercise write
# actions that now each log a real `ModerationLogEntry` row. Same
# "JSONB has no SQLite DDL compiler" raw-SQL pattern as `notifications`
# above.
_CREATE_MODERATION_LOG_TABLE_SQL = """
CREATE TABLE moderation_log_entries (
    id VARCHAR(36) PRIMARY KEY,
    actor_id VARCHAR(36),
    actor_role VARCHAR(20) NOT NULL,
    action VARCHAR(30) NOT NULL,
    target_type VARCHAR(20) NOT NULL,
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
    )
    async with engine.begin() as conn:
        await conn.execute(text(_CREATE_USERS_TABLE_SQL))
        await conn.execute(text(_CREATE_USER_BOOKMARKS_TABLE_SQL))
        await conn.execute(text(_CREATE_STUDENT_VIDEOS_TABLE_SQL))
        await conn.execute(text(_CREATE_SOURCES_TABLE_SQL))
        await conn.execute(text(_CREATE_OPPORTUNITIES_TABLE_SQL))
        await conn.execute(text(_CREATE_MODERATION_LOG_TABLE_SQL))
        # DISCLOSED, NECESSARY FIX (found running the full suite during the
        # Real Leaderboard System task -- unrelated to that task's own
        # changes, a pre-existing regression from an EARLIER migration):
        # this file's own module docstring above claims `notifications` is
        # one of the tables where "none of the four [models] have a
        # JSONB/ARRAY column, so the ORM's own real DDL compiles fine on
        # SQLite unmodified" -- true when written, no longer true since
        # migration 0033 added a real `JSONB` column, `Notification.
        # payload` (Actionable Notifications), which SQLite's DDL compiler
        # cannot render (`CompileError`). Same fix as `_CREATE_USERS_TABLE_
        # SQL` already uses for `User.skills` (a Postgres `ARRAY`): a
        # raw-SQL `CREATE TABLE`, `payload` as plain `TEXT`.
        await conn.execute(text(_CREATE_NOTIFICATIONS_TABLE_SQL))
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                GamificationProfile.__table__,
                Badge.__table__,
                UserBadge.__table__,
                DangerousActionConfirmation.__table__,
            ],
        )

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


async def _make_user(
    db: AsyncSession, *, is_superuser: bool = False, age_group: str | None = None, full_name: str = "Test User"
) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@example.com",
        full_name=full_name,
        is_superuser=is_superuser,
        age_group=age_group,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_video(db: AsyncSession, *, student_id: uuid.UUID, status: str, **overrides) -> StudentVideo:
    defaults = dict(
        id=overrides.pop("id", uuid.uuid4()),
        student_id=student_id,
        title="How Photosynthesis Works",
        status=status,
        ai_safety_flag=False,
    )
    defaults.update(overrides)
    video = StudentVideo(**defaults)
    db.add(video)
    await db.commit()
    await db.refresh(video)
    return video


async def _make_opportunity(db: AsyncSession, *, source_id: uuid.UUID, **overrides) -> uuid.UUID:
    """Raw INSERT, not the ORM -- `Opportunity.embedding` is a pgvector
    column with no SQLite equivalent, same reasoning `tests/
    test_aggregator.py` already established for its own opportunity
    fixtures.

    DISCLOSED, NECESSARY UUID ENCODING FIX -- found and fixed while
    writing this test, not assumed: `Opportunity.id`/`source_id` are
    `postgresql.UUID(as_uuid=True)` columns. On a non-Postgres dialect
    (SQLite here), that type's `bind_processor` encodes a Python `UUID`
    as its 32-character DASHLESS hex form (`uuid.hex`) for any query the
    ORM itself constructs (e.g. the `source` relationship's own
    `WHERE sources.id = ?` lookup) -- confirmed directly by inspecting
    the processor. A raw-SQL `INSERT` using `str(uuid)` (the DASHED
    form) stores a byte-for-byte DIFFERENT string in the same VARCHAR
    column, so the ORM's later equality comparison silently never
    matches (`opportunity.source` reads back as `None` -- not an error,
    just a silently broken relationship). `.hex` is used for every
    UUID-typed column value below and in `_make_source` so raw-SQL rows
    are byte-for-byte consistent with what the ORM itself would have
    written, and `opportunity.source`/`OpportunityOut.source` resolve
    correctly.
    """
    opportunity_id = overrides.pop("id", uuid.uuid4())
    defaults = {
        "id": opportunity_id.hex,
        "title": "Some Opportunity",
        "target_url": "https://example.com/apply",
        "source_id": source_id.hex,
        # `Opportunity.type` is a native Postgres/SQLAlchemy `Enum`
        # (`app/models/opportunity.py`'s `SAEnum(OpportunityType, ...)`),
        # which stores/reads back the enum MEMBER NAME by default (e.g.
        # `"SCHOLARSHIP"`), not `.value` (`"scholarship"`) -- confirmed
        # directly (a lowercase raw insert here raised a real
        # `LookupError` on read-back the first time this test ran).
        # Lowercase, not the Python enum MEMBER NAME -- pre-existing bug in
        # this fixture found while running the full suite for the Real
        # Age/Academic-Year/Gender Opportunity Filtering step (unrelated to
        # that step's own changes): `Opportunity.type`'s own column comment
        # already documents the real `values_callable` fix requiring
        # lowercase values here; this fixture just hadn't been updated.
        "type": "scholarship",
        "deadline": None,
        "is_local_egypt": True,
        "target_age_group": None,
    }
    defaults.update(overrides)
    await db.execute(
        text(
            """
            INSERT INTO opportunities
                (id, title, target_url, source_id, type, deadline, is_local_egypt, target_age_group)
            VALUES
                (:id, :title, :target_url, :source_id, :type, :deadline, :is_local_egypt, :target_age_group)
            """
        ),
        defaults,
    )
    await db.commit()
    return opportunity_id


async def _make_source(db: AsyncSession) -> uuid.UUID:
    source_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO sources (id, name, base_url) VALUES (:id, :name, :base_url)"
        ),
        {"id": source_id.hex, "name": "Test Source", "base_url": "https://example.com"},
    )
    await db.commit()
    return source_id


@pytest.fixture
def client_factory(db_session_factory):
    """Same `(TestClient, overrides-cleared-on-teardown)` technique as
    `tests/test_admin_moderation.py`'s own `client_factory`."""

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


# ---------------------------------------------------------------------
# Fake Redis -- see module docstring.
# ---------------------------------------------------------------------


class _FakeRedis:
    def __init__(self) -> None:
        self._store: dict[str, tuple[str, int | None]] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._store[key] = (value, ex)

    async def exists(self, key: str) -> int:
        return 1 if key in self._store else 0

    async def ttl(self, key: str) -> int:
        if key not in self._store:
            return -2
        _, ex = self._store[key]
        return ex if ex is not None else -1

    async def delete(self, key: str) -> int:
        return 1 if self._store.pop(key, None) is not None else 0

    async def scan_iter(self, match: str):
        # Real redis-py `scan_iter` glob-matches; this codebase's own
        # usage only ever passes a literal `"prefix*"` pattern, so a
        # plain `startswith` on the stripped trailing `*` is a faithful,
        # sufficient stand-in (same "implement only what's actually
        # used" posture as `tests/test_chat.py`'s own `_FakeRedis`).
        prefix = match.rstrip("*")
        for key in list(self._store.keys()):
            if key.startswith(prefix):
                yield key


@pytest.fixture
def fake_redis(monkeypatch):
    redis = _FakeRedis()
    monkeypatch.setattr(chat_security, "_get_redis_client", lambda: redis)
    return redis


# ---------------------------------------------------------------------
# New/extended service-layer functions
# ---------------------------------------------------------------------


class TestChatSecurityAdditions:
    async def test_list_banned_users_returns_every_active_ban(self, fake_redis):
        alice, bob = uuid.uuid4(), uuid.uuid4()
        await chat_security.ban_user(alice, duration_seconds=900)
        await chat_security.ban_user(bob, duration_seconds=1800)

        banned = await chat_security.list_banned_users()

        assert {user_id for user_id, _ in banned} == {alice, bob}
        remaining_by_user = dict(banned)
        assert remaining_by_user[alice] == 900
        assert remaining_by_user[bob] == 1800

    async def test_list_banned_users_is_empty_when_nobody_is_banned(self, fake_redis):
        assert await chat_security.list_banned_users() == []

    async def test_unban_user_removes_the_key_and_reports_it_was_banned(self, fake_redis):
        user_id = uuid.uuid4()
        await chat_security.ban_user(user_id)

        was_banned = await chat_security.unban_user(user_id)

        assert was_banned is True
        assert await chat_security.is_user_banned(user_id) is False
        assert await chat_security.list_banned_users() == []

    async def test_unban_user_on_a_user_who_isnt_banned_returns_false(self, fake_redis):
        assert await chat_security.unban_user(uuid.uuid4()) is False


class TestNotificationUnreadOnly:
    async def test_unread_only_excludes_already_read_notifications(self, db_session_factory):
        async with db_session_factory() as db:
            user = await _make_user(db)
            n1 = await notification_service.create_notification(db, user.id, "SYSTEM", "One", "body")
            await notification_service.create_notification(db, user.id, "SYSTEM", "Two", "body")
            await notification_service.mark_notification_read(db, user.id, n1.id)

            unread = await notification_service.list_notifications(db, user.id, unread_only=True)

            assert [n.title for n in unread] == ["Two"]

    async def test_unread_only_false_by_default_returns_everything(self, db_session_factory):
        async with db_session_factory() as db:
            user = await _make_user(db)
            await notification_service.create_notification(db, user.id, "SYSTEM", "One", "body")
            await notification_service.create_notification(db, user.id, "SYSTEM", "Two", "body")

            everything = await notification_service.list_notifications(db, user.id)

            assert len(everything) == 2


class TestNextBadgeThreshold:
    async def test_returns_the_lowest_still_locked_threshold(self, db_session_factory):
        async with db_session_factory() as db:
            db.add_all(
                [
                    Badge(id=uuid.uuid4(), name="First Steps", description="d", required_xp=100),
                    Badge(id=uuid.uuid4(), name="Scholar", description="d", required_xp=500),
                    Badge(id=uuid.uuid4(), name="Legend", description="d", required_xp=1000),
                ]
            )
            await db.commit()

            next_badge = await gamification_service.get_next_badge_threshold(db, total_xp=250)

            assert next_badge is not None
            assert next_badge.name == "Scholar"
            assert next_badge.required_xp == 500

    async def test_returns_none_once_every_badge_is_crossed(self, db_session_factory):
        async with db_session_factory() as db:
            db.add(Badge(id=uuid.uuid4(), name="Only Badge", description="d", required_xp=100))
            await db.commit()

            next_badge = await gamification_service.get_next_badge_threshold(db, total_xp=500)

            assert next_badge is None


# ---------------------------------------------------------------------
# GET /api/v1/dashboards/student
# ---------------------------------------------------------------------


class TestStudentDashboard:
    async def test_aggregates_gamification_notifications_videos_and_opportunities(
        self, db_session_factory, client_factory
    ):
        async with db_session_factory() as db:
            student = await _make_user(db, age_group="university", full_name="Jamie Student")

            # Gamification: award XP, which also unlocks a badge.
            db.add(Badge(id=uuid.uuid4(), name="First Steps", description="d", required_xp=50))
            db.add(Badge(id=uuid.uuid4(), name="Scholar", description="d", required_xp=1000))
            await db.commit()
            await gamification_service.process_daily_activity(db, student.id)
            await gamification_service.add_xp(db, student.id, 75, "daily_login")

            # Notifications: 2 unread, 1 already read -- only the 2 unread
            # should surface, newest first.
            await notification_service.create_notification(db, student.id, "SYSTEM", "Older unread", "body")
            newest = await notification_service.create_notification(
                db, student.id, "ACHIEVEMENT", "Newest unread", "body"
            )
            already_read = await notification_service.create_notification(
                db, student.id, "SYSTEM", "Already read", "body"
            )
            await notification_service.mark_notification_read(db, student.id, already_read.id)

            # Videos: one of each status -- all should appear (no status filter).
            await _make_video(db, student_id=student.id, status=StudentVideoStatus.APPROVED.value)
            await _make_video(db, student_id=student.id, status=StudentVideoStatus.PENDING_AI.value)

            # Opportunities: local+matching age (visible), local+no-age-set
            # (visible, broadly applicable), local+wrong-age (excluded),
            # non-local (excluded).
            source_id = await _make_source(db)
            visible_matching = await _make_opportunity(
                db, source_id=source_id, title="Cairo University Scholarship", target_age_group="university"
            )
            visible_broad = await _make_opportunity(
                db, source_id=source_id, title="Open to All", target_age_group=None
            )
            await _make_opportunity(
                db, source_id=source_id, title="Wrong Age Group", target_age_group="prep_secondary"
            )
            await _make_opportunity(
                db, source_id=source_id, title="Not Local", is_local_egypt=False
            )

        client = client_factory(student)
        response = client.get("/api/v1/dashboards/student")

        assert response.status_code == 200
        body = response.json()

        assert body["gamification"]["total_xp"] == 75
        assert body["gamification"]["current_streak"] == 1
        assert len(body["gamification"]["badges"]) == 1
        assert body["gamification"]["badges"][0]["name"] == "First Steps"
        assert body["gamification"]["next_badge_xp"] == 1000

        # `add_xp` above (75 XP, crossing the 50-XP "First Steps"
        # threshold) ALSO fires a real "New badge unlocked!" notification
        # of its own (`gamification_service.add_xp`'s own Smart
        # Notifications Engine wiring -- not mocked out here, exercised
        # for real) -- a genuine third unread notification, oldest of
        # the three (created first, before the two explicit ones below),
        # so it's the third and last item within this endpoint's own
        # top-3 limit.
        notification_titles = [n["title"] for n in body["recent_notifications"]]
        assert notification_titles == ["Newest unread", "Older unread", "New badge unlocked!"]
        assert all(n["is_read"] is False for n in body["recent_notifications"])

        assert len(body["videos"]) == 2
        video_statuses = {v["status"] for v in body["videos"]}
        assert video_statuses == {"approved", "pending_ai"}

        opportunity_ids = {o["id"] for o in body["opportunities"]}
        assert opportunity_ids == {str(visible_matching), str(visible_broad)}

    async def test_opportunities_exclude_expired_deadlines(self, db_session_factory, client_factory):
        from datetime import datetime, timedelta, timezone

        async with db_session_factory() as db:
            student = await _make_user(db)
            source_id = await _make_source(db)
            # Real `datetime` objects (not ISO strings) -- SQLAlchemy's
            # SQLite `DateTime` type has its own bind/result processors
            # that expect a specific naive-string format, not a
            # timezone-aware ISO string with a `T` separator and UTC
            # offset; passing a real `datetime` through the bound
            # parameter lets the dialect handle serialization correctly,
            # same posture as `tests/test_aggregator.py`'s own ORM-level
            # `deadline=datetime(2026, 8, 30, tzinfo=timezone.utc)` fixture.
            await _make_opportunity(
                db,
                source_id=source_id,
                title="Expired",
                deadline=datetime.now(timezone.utc) - timedelta(days=1),
            )
            future = await _make_opportunity(
                db,
                source_id=source_id,
                title="Still Open",
                deadline=datetime.now(timezone.utc) + timedelta(days=30),
            )

        client = client_factory(student)
        response = client.get("/api/v1/dashboards/student")

        assert response.status_code == 200
        opportunity_ids = {o["id"] for o in response.json()["opportunities"]}
        assert opportunity_ids == {str(future)}

    async def test_new_student_gets_zeroed_gamification_not_a_404(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)

        client = client_factory(student)
        response = client.get("/api/v1/dashboards/student")

        assert response.status_code == 200
        body = response.json()
        assert body["gamification"]["total_xp"] == 0
        assert body["gamification"]["current_streak"] == 0
        assert body["gamification"]["badges"] == []
        assert body["recent_notifications"] == []
        assert body["videos"] == []


# ---------------------------------------------------------------------
# GET /api/v1/dashboards/admin
# ---------------------------------------------------------------------


class TestAdminDashboard:
    async def test_non_admin_gets_403(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            student = await _make_user(db, is_superuser=False)

        client = client_factory(student)
        response = client.get("/api/v1/dashboards/admin")

        assert response.status_code == 403

    async def test_aggregates_moderation_queue_banned_users_and_platform_stats(
        self, db_session_factory, client_factory, fake_redis
    ):
        async with db_session_factory() as db:
            admin = await _make_user(db, is_superuser=True, full_name="Admin Person")
            student = await _make_user(db, is_superuser=False, full_name="Uploader Student")
            banned_user = await _make_user(db, is_superuser=False, full_name="Banned Student")

            # Moderation queue: REJECTED/FAILED/PENDING_AI show up, APPROVED does not.
            rejected = await _make_video(db, student_id=student.id, status=StudentVideoStatus.REJECTED.value)
            # `duration_seconds=5400` (1.5 hours) -- feeds the platform
            # stats' `total_active_video_hours` SUM below; this is the
            # only APPROVED row in this test, so the expected total is
            # exactly 1.5.
            await _make_video(
                db,
                student_id=student.id,
                status=StudentVideoStatus.APPROVED.value,
                duration_seconds=5400,
            )

            source_id = await _make_source(db)
            await _make_opportunity(db, source_id=source_id, title="Opp 1")
            await _make_opportunity(db, source_id=source_id, title="Opp 2")

        await chat_security.ban_user(banned_user.id, duration_seconds=1200)

        client = client_factory(admin)
        response = client.get("/api/v1/dashboards/admin")

        assert response.status_code == 200
        body = response.json()

        queue_ids = {item["id"] for item in body["moderation_queue"]}
        assert queue_ids == {str(rejected.id)}
        assert body["moderation_queue"][0]["student_name"] == "Uploader Student"

        assert len(body["banned_users"]) == 1
        assert body["banned_users"][0]["user_id"] == str(banned_user.id)
        assert body["banned_users"][0]["display_name"] == "Banned Student"
        assert body["banned_users"][0]["remaining_seconds"] == 1200

        stats = body["platform_stats"]
        assert stats["total_users"] == 3
        # 5400 seconds (the sole APPROVED video's duration_seconds) / 3600 == 1.5 hours.
        assert stats["total_active_video_hours"] == 1.5
        assert stats["total_scraped_opportunities"] == 2

    async def test_platform_stats_video_hours_is_zero_when_no_approved_videos_have_a_duration(
        self, db_session_factory, client_factory, fake_redis
    ):
        """DISCLOSED, NECESSARY COVERAGE for the `func.coalesce(SUM(...), 0)`
        guard: an APPROVED video with a NULL `duration_seconds` (the
        `_make_video` helper's own default -- no `duration_seconds`
        override) must not turn the whole SUM into SQL NULL, which would
        otherwise 500 at the Pydantic `float` validation step instead of
        returning `0.0`."""
        async with db_session_factory() as db:
            admin = await _make_user(db, is_superuser=True)
            student = await _make_user(db, is_superuser=False)
            await _make_video(db, student_id=student.id, status=StudentVideoStatus.APPROVED.value)

        client = client_factory(admin)
        response = client.get("/api/v1/dashboards/admin")

        assert response.status_code == 200
        assert response.json()["platform_stats"]["total_active_video_hours"] == 0.0


# ---------------------------------------------------------------------
# New admin action endpoints
# ---------------------------------------------------------------------


class TestRejectAndPurgeMedia:
    async def test_rejects_and_clears_media_urls_and_dispatches_cleanup(
        self, db_session_factory, client_factory, monkeypatch
    ):
        """Real Two-Person Confirmation for Dangerous Actions addition:
        `reject-and-purge-media` is one of the four real irreversible
        actions -- the original POST now only REQUESTS the action (202,
        pending); the real R2 purge dispatch happens at approval time,
        from `dangerous_action_service.py` (where the mutation logic
        actually lives now -- see that module's own docstring), by a
        real SECOND admin, never the original requester."""
        dispatched: list[list[str]] = []

        from app.services import dangerous_action_service as dangerous_action_service_module

        monkeypatch.setattr(
            dangerous_action_service_module.task_delete_orphaned_media,
            "delay",
            lambda r2_urls: dispatched.append(r2_urls),
        )

        async with db_session_factory() as db:
            admin = await _make_user(db, is_superuser=True)
            second_admin = await _make_user(db, is_superuser=True)
            student = await _make_user(db, is_superuser=False)
            video = await _make_video(
                db,
                student_id=student.id,
                status=StudentVideoStatus.PENDING_AI.value,
                hls_master_url="https://r2.example.com/hls/master.m3u8",
                thumbnail_url="https://r2.example.com/thumb.jpg",
            )

        client = client_factory(admin)
        response = client.post(
            f"/api/v1/admin/videos/{video.id}/reject-and-purge-media",
            json={"notes": "Inappropriate content."},
        )

        assert response.status_code == 202
        confirmation_id = response.json()["confirmation_id"]

        approver_client = client_factory(second_admin)
        approve_response = approver_client.post(
            f"/api/v1/admin/moderation/dangerous-actions/{confirmation_id}/approve"
        )
        assert approve_response.status_code == 200
        approve_body = approve_response.json()
        assert approve_body["status"] == "executed"

        assert dispatched == [
            ["https://r2.example.com/hls/master.m3u8", "https://r2.example.com/thumb.jpg"]
        ]

        async with db_session_factory() as db:
            persisted = await db.get(StudentVideo, video.id)
            assert persisted.status == StudentVideoStatus.REJECTED.value
            assert persisted.admin_override is True
            assert persisted.admin_notes == "Inappropriate content."
            assert persisted.hls_master_url is None
            assert persisted.thumbnail_url is None

    async def test_404_for_nonexistent_video(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            admin = await _make_user(db, is_superuser=True)

        client = client_factory(admin)
        response = client.post(
            f"/api/v1/admin/videos/{uuid.uuid4()}/reject-and-purge-media", json={}
        )

        assert response.status_code == 404


class TestAdminUnban:
    async def test_admin_can_unban_a_currently_banned_user(self, db_session_factory, client_factory, fake_redis):
        async with db_session_factory() as db:
            admin = await _make_user(db, is_superuser=True)

        target_user_id = uuid.uuid4()
        await chat_security.ban_user(target_user_id, duration_seconds=1800)

        client = client_factory(admin)
        response = client.delete(f"/api/v1/chat/admin/bans/{target_user_id}")

        assert response.status_code == 200
        body = response.json()
        assert body["was_banned"] is True
        assert await chat_security.is_user_banned(target_user_id) is False

    async def test_non_admin_gets_403(self, db_session_factory, client_factory, fake_redis):
        async with db_session_factory() as db:
            student = await _make_user(db, is_superuser=False)

        client = client_factory(student)
        response = client.delete(f"/api/v1/chat/admin/bans/{uuid.uuid4()}")

        assert response.status_code == 403
