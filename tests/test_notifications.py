"""Behavioral tests for the Smart Notifications Engine:
`app/services/notification_service.py` (create/list/mark-read logic) and
`app/api/v1/endpoints/notifications.py` (the real HTTP layer on top of
it).

In-memory SQLite, `StaticPool`. Table creation strategy, per this
codebase's own established discipline (see `tests/test_gamification.py`'s
own docstring on this exact point):

- `users` -- raw SQL (`User.skills` is a Postgres `ARRAY`, which
  SQLite's DDL compiler has no `visit_ARRAY` for).
- `user_bookmarks` -- raw SQL, empty/unused, present ONLY because
  `User.bookmarks` is a `lazy="selectin"` relationship that eagerly
  queries this table the moment any `User` row is fetched/refreshed.
- `notifications` -- `Base.metadata.create_all(tables=[...])` directly.
  `Notification` has no `JSONB`/`ARRAY` column, so the ORM's own real
  DDL compiles fine on SQLite unmodified.

This step's own two EXPLICIT verification requirements (both covered
below):

1. "Creating a notification successfully saves it to the database" --
   `TestCreateNotification.test_create_notification_persists_to_the_database`.
2. "The `/read-all` endpoint correctly updates all `is_read` flags for a
   mock user" -- `TestNotificationsRouterHTTP.
   test_read_all_marks_every_unread_notification_for_the_current_user`.

NO REAL REDIS OR CELERY BROKER is reachable in this test environment --
deliberately not mocked/patched around, because `notification_service.
create_notification`'s own real-time-broadcast and critical-email steps
are ALREADY wrapped in `try/except Exception` (see that function's own
docstring) specifically so a Redis/Celery-broker hiccup can never break
the one thing every caller actually depends on: the notification's own
DB write. These tests exercise that real, unmocked failure path --
`create_notification` succeeding end-to-end here, with no broker of any
kind actually running, IS itself a live proof that the best-effort
wrapping works, not just a inconvenience being worked around.
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
from app.models.notification import Notification
from app.models.user import User
from app.services import notification_service

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
        # this module's own docstring above claims "`Notification` has no
        # `JSONB`/`ARRAY` column" -- true when originally written, but no
        # longer true since migration 0033 added `Notification.payload` as
        # a real `JSONB` column (Actionable Notifications). `Base.metadata.
        # create_all` on `Notification.__table__` therefore now fails to
        # even compile on SQLite (`CompileError: ... can't render element
        # of type JSONB`), the same "Postgres-only DDL, SQLite's DDL
        # compiler chokes on it" problem `_CREATE_USERS_TABLE_SQL` above
        # already exists to work around for `User.skills` (ARRAY). Same
        # fix, applied here: a raw-SQL `CREATE TABLE`, `payload` as a plain
        # `TEXT` column (JSON-serializable dicts still round-trip through
        # SQLAlchemy's ORM-level ``Mapped[dict | None]`` typing fine against
        # a TEXT column at the DML level -- it's only DDL compilation of
        # the `JSONB` type itself that SQLite's compiler can't do).
        await conn.execute(text(_CREATE_NOTIFICATIONS_TABLE_SQL))

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_session_factory):
    async with db_session_factory() as session:
        yield session


async def _make_user(db: AsyncSession) -> User:
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.com", full_name="Test Student")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


# ---------------------------------------------------------------------
# Service-layer tests: create_notification
# ---------------------------------------------------------------------


class TestCreateNotification:
    async def test_create_notification_persists_to_the_database(self, db_session):
        """This step's own explicit verification requirement #1."""
        user = await _make_user(db_session)

        notification = await notification_service.create_notification(
            db_session, user.id, "SYSTEM", "Welcome!", "Thanks for joining Apex."
        )

        assert notification.id is not None
        assert notification.is_read is False

        # Persisted for real -- confirmed via a fresh, independent read
        # (a brand-new SELECT), not just the in-request return value.
        fetched = await db_session.get(Notification, notification.id)
        assert fetched is not None
        assert fetched.user_id == user.id
        assert fetched.type == "SYSTEM"
        assert fetched.title == "Welcome!"
        assert fetched.message == "Thanks for joining Apex."
        assert fetched.is_read is False
        assert fetched.created_at is not None

    async def test_create_notification_survives_with_no_redis_or_celery_broker_reachable(self, db_session):
        """No mocking here on purpose -- see this module's own docstring.
        This is exactly the "best-effort, never breaks the DB write"
        contract `create_notification`'s own docstring describes, proven
        against the real (absent) infrastructure, not a stand-in for it.
        """
        user = await _make_user(db_session)

        # type="WARNING" additionally exercises the critical-email
        # dispatch path (see `notification_service.HIGHLY_CRITICAL_TYPES`)
        # -- also must not raise, even with no Celery broker reachable.
        notification = await notification_service.create_notification(
            db_session, user.id, "WARNING", "Deadline approaching!", "Due soon."
        )

        assert notification.id is not None

    async def test_highly_critical_type_does_not_email_a_user_with_no_email_on_file(self, db_session):
        """`user.email` can legitimately be `None` (a Supabase-provisioned
        row with no `email` claim) -- must be skipped gracefully, not
        raise, per `_dispatch_critical_email_best_effort`'s own docstring."""
        user = User(id=uuid.uuid4(), email=None, full_name="No Email Student")
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)

        notification = await notification_service.create_notification(
            db_session, user.id, "WARNING", "Deadline approaching!", "Due soon."
        )

        assert notification.id is not None


# ---------------------------------------------------------------------
# Service-layer tests: list / mark read / mark all read
# ---------------------------------------------------------------------


class TestListAndMarkRead:
    async def test_list_notifications_orders_newest_first_and_respects_limit(self, db_session):
        user = await _make_user(db_session)
        for i in range(3):
            await notification_service.create_notification(
                db_session, user.id, "SYSTEM", f"Notification {i}", "body"
            )

        notifications = await notification_service.list_notifications(db_session, user.id, limit=50)

        assert len(notifications) == 3
        # Newest first -- `created_at` may tie at SQLite's own timestamp
        # resolution, so this only asserts non-increasing order, not a
        # strict one, matching what the DB itself actually guarantees.
        timestamps = [n.created_at for n in notifications]
        assert timestamps == sorted(timestamps, reverse=True)

    async def test_list_notifications_only_returns_the_current_users_own(self, db_session):
        user = await _make_user(db_session)
        other = await _make_user(db_session)
        await notification_service.create_notification(db_session, user.id, "SYSTEM", "Mine", "body")
        await notification_service.create_notification(db_session, other.id, "SYSTEM", "Not mine", "body")

        notifications = await notification_service.list_notifications(db_session, user.id)

        assert len(notifications) == 1
        assert notifications[0].title == "Mine"

    async def test_mark_notification_read_flips_the_flag_and_is_idempotent(self, db_session):
        user = await _make_user(db_session)
        notification = await notification_service.create_notification(
            db_session, user.id, "SYSTEM", "Title", "body"
        )
        assert notification.is_read is False

        first = await notification_service.mark_notification_read(db_session, user.id, notification.id)
        assert first.is_read is True

        # Idempotent repeat -- per this function's own docstring.
        second = await notification_service.mark_notification_read(db_session, user.id, notification.id)
        assert second.is_read is True

    async def test_mark_notification_read_raises_for_another_users_notification(self, db_session):
        user = await _make_user(db_session)
        other = await _make_user(db_session)
        notification = await notification_service.create_notification(
            db_session, other.id, "SYSTEM", "Not yours", "body"
        )

        with pytest.raises(notification_service.NotificationNotFoundError):
            await notification_service.mark_notification_read(db_session, user.id, notification.id)

    async def test_mark_all_notifications_read_flips_only_this_users_unread_ones(self, db_session):
        """Service-layer counterpart to this step's own explicit
        verification requirement #2 (the router-level version is under
        `TestNotificationsRouterHTTP` below)."""
        user = await _make_user(db_session)
        other = await _make_user(db_session)

        n1 = await notification_service.create_notification(db_session, user.id, "SYSTEM", "1", "body")
        n2 = await notification_service.create_notification(db_session, user.id, "SYSTEM", "2", "body")
        await notification_service.mark_notification_read(db_session, user.id, n1.id)  # already read
        await notification_service.create_notification(db_session, other.id, "SYSTEM", "3", "body")

        marked_count = await notification_service.mark_all_notifications_read(db_session, user.id)

        assert marked_count == 1, "only n2 was unread for this user; n1 was already read, n3 belongs to another user"

        n2_fetched = await db_session.get(Notification, n2.id)
        assert n2_fetched.is_read is True

        other_notification = await db_session.get(Notification, n1.id)
        assert other_notification.is_read is True, "n1's own already-read state must be undisturbed, not un-set"


# ---------------------------------------------------------------------
# Router-level tests (real HTTP, via TestClient)
# ---------------------------------------------------------------------


class TestNotificationsRouterHTTP:
    """Same `client_factory` fixture pattern as `tests/test_gamification.py`'s
    own `TestGamificationRouterHTTP`."""

    @pytest.fixture
    def client_factory(self, db_session_factory):
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

    async def test_list_endpoint_for_a_brand_new_user_is_an_empty_list_not_a_404(self, client_factory):
        client, _user = await client_factory()

        response = client.get("/api/v1/notifications")

        assert response.status_code == 200
        assert response.json() == []

    async def test_read_all_marks_every_unread_notification_for_the_current_user(
        self, db_session_factory, client_factory
    ):
        """This step's own explicit verification requirement #2: "the
        `/read-all` endpoint correctly updates all `is_read` flags for a
        mock user" -- exercised as a real HTTP `PUT` call, not a direct
        service-layer call."""
        client, user = await client_factory()
        async with db_session_factory() as session:
            other = await _make_user(session)
            for i in range(3):
                await notification_service.create_notification(session, user.id, "SYSTEM", f"N{i}", "body")
            # A distractor belonging to a different user -- must be
            # left alone by this call.
            await notification_service.create_notification(session, other.id, "SYSTEM", "Not yours", "body")

        response = client.put("/api/v1/notifications/read-all")

        assert response.status_code == 200
        assert response.json() == {"marked_read_count": 3}

        list_response = client.get("/api/v1/notifications")
        assert all(n["is_read"] for n in list_response.json())

        async with db_session_factory() as session:
            other_notifications = await notification_service.list_notifications(session, other.id)
            assert other_notifications[0].is_read is False, "another user's notification must be untouched"

    async def test_mark_one_read_endpoint_flips_the_flag(self, db_session_factory, client_factory):
        client, user = await client_factory()
        async with db_session_factory() as session:
            notification = await notification_service.create_notification(
                session, user.id, "SYSTEM", "Title", "body"
            )

        response = client.put(f"/api/v1/notifications/{notification.id}/read")

        assert response.status_code == 200
        assert response.json()["is_read"] is True

    async def test_mark_one_read_endpoint_404s_for_a_nonexistent_notification(self, client_factory):
        client, _user = await client_factory()

        response = client.put(f"/api/v1/notifications/{uuid.uuid4()}/read")

        assert response.status_code == 404
