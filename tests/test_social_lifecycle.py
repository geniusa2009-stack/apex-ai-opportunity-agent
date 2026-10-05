"""NETWORK 2.0, Phase 6/14: functional (non-concurrency) coverage of the
`request_friend` lifecycle fix and the pre-existing `respond_to_friend_
request` authorization guard, now actually exercised by a dedicated
test rather than only described in a docstring (see `test_privacy.py`'s
`TestAdversarial` class docstring, which claimed this guard was "audited
... in the prior pass" -- true of the code, not of test coverage; this
file closes that gap).

Same `StaticPool` in-memory SQLite convention as `test_social_
performance.py` -- this file runs operations sequentially within a
single test, never two genuinely concurrent transactions, so `StaticPool`
is the correct engine here (see `test_social_concurrency.py`'s module
docstring for why it is NOT the right tool for real concurrency testing,
which is exactly why that is a separate file).
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models.social import UserConnection
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
    role VARCHAR(20) NOT NULL DEFAULT \'student\',
    admin_role VARCHAR(20),
    role_granted_at DATETIME,
    nationality VARCHAR(120), residency_country VARCHAR(120), languages TEXT,
    interests TEXT, application_goals TEXT, preferred_opportunity_types TEXT,
    remote_preference BOOLEAN, funding_preference VARCHAR(30), deadline_lead_time_days INTEGER, date_of_birth DATE, english_level VARCHAR(20), other_nationalities TEXT, onboarding_completed_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

# `User.bookmarks` is `lazy="selectin"` -- every ORM load of a `User`
# row issues an automatic follow-up SELECT against this table (see
# `test_privacy.py`'s identical fixture comment for the full story).
_CREATE_USER_BOOKMARKS_SQL = """
CREATE TABLE user_bookmarks (
    id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36) NOT NULL, opportunity_id VARCHAR(36),
    course_id VARCHAR(36), status VARCHAR(20) NOT NULL DEFAULT \'saved\',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_USER_CONNECTIONS_SQL = """
CREATE TABLE user_connections (
    id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36) NOT NULL, friend_id VARCHAR(36) NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'pending',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

# `request_friend` fires a best-effort notification on every new/
# re-opened request -- `notification_service.create_notification` writes
# to this table. Wrapped in `try/except Exception` in the service itself
# (never fails the request), but the table must still exist for the
# happy path to actually write one, so tests that assert on lifecycle
# behavior aren't silently masking a notification-layer failure.
_CREATE_NOTIFICATIONS_SQL = """
CREATE TABLE notifications (
    id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36) NOT NULL, type VARCHAR(50) NOT NULL,
    title VARCHAR(255) NOT NULL, message TEXT, payload TEXT,
    is_read BOOLEAN NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.execute(text(_CREATE_USERS_SQL))
        await conn.execute(text(_CREATE_USER_CONNECTIONS_SQL))
        await conn.execute(text(_CREATE_NOTIFICATIONS_SQL))
        await conn.execute(text(_CREATE_USER_BOOKMARKS_SQL))

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


def _user(**overrides) -> User:
    defaults = dict(id=uuid.uuid4(), email=f"{uuid.uuid4().hex}@example.com", full_name="Test User")
    defaults.update(overrides)
    return User(**defaults)


async def _single_edge(db, user_id, friend_id) -> UserConnection:
    result = await db.execute(
        select(UserConnection).where(
            UserConnection.user_id == user_id, UserConnection.friend_id == friend_id
        )
    )
    return result.scalar_one()


class TestRequestFriendLifecycle:
    """The real bug this phase found and fixed: a `declined` connection
    (in either direction) used to permanently block the original sender
    from ever re-requesting that person, contradicting the directive's
    own "DECLINE -> NO CONNECTION" state diagram."""

    @pytest.mark.asyncio
    async def test_first_time_request_creates_a_pending_row(self, db_session):
        a, b = _user(), _user()
        db_session.add_all([a, b])
        await db_session.commit()

        connection = await social_service.request_friend(db_session, a.id, b.id)
        assert connection.status == "pending"
        assert connection.user_id == a.id
        assert connection.friend_id == b.id

    @pytest.mark.asyncio
    async def test_a_still_pending_duplicate_request_raises(self, db_session):
        a, b = _user(), _user()
        db_session.add_all([a, b])
        await db_session.commit()
        await social_service.request_friend(db_session, a.id, b.id)

        with pytest.raises(social_service.DuplicateConnectionError):
            await social_service.request_friend(db_session, a.id, b.id)

    @pytest.mark.asyncio
    async def test_requesting_an_already_accepted_connection_is_idempotent(self, db_session):
        a, b = _user(), _user()
        db_session.add_all([a, b])
        await db_session.flush()
        db_session.add(UserConnection(id=uuid.uuid4(), user_id=a.id, friend_id=b.id, status="accepted"))
        await db_session.commit()

        # No exception -- returns the existing accepted row as-is.
        connection = await social_service.request_friend(db_session, a.id, b.id)
        assert connection.status == "accepted"

        rows = (await db_session.execute(select(UserConnection))).scalars().all()
        assert len(rows) == 1  # no duplicate row created

    @pytest.mark.asyncio
    async def test_mutual_request_auto_accepts_instead_of_creating_a_second_row(self, db_session):
        """B already sent A a pending request; A now requests B back --
        must resolve to a single accepted connection, not two permanently
        pending rows."""
        a, b = _user(), _user()
        db_session.add_all([a, b])
        await db_session.flush()
        db_session.add(UserConnection(id=uuid.uuid4(), user_id=b.id, friend_id=a.id, status="pending"))
        await db_session.commit()

        connection = await social_service.request_friend(db_session, a.id, b.id)
        assert connection.status == "accepted"

        rows = (await db_session.execute(select(UserConnection))).scalars().all()
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_declined_forward_connection_becomes_re_requestable(self, db_session):
        """THE LIFECYCLE FIX, forward direction: A requested B, B
        declined. A must be able to request B again -- a fresh, genuine
        ask, not a permanent dead end -- and the OLD row is reused
        (flipped back to pending), not duplicated."""
        a, b = _user(), _user()
        db_session.add_all([a, b])
        await db_session.flush()
        declined_id = uuid.uuid4()
        db_session.add(UserConnection(id=declined_id, user_id=a.id, friend_id=b.id, status="declined"))
        await db_session.commit()

        connection = await social_service.request_friend(db_session, a.id, b.id)
        assert connection.status == "pending"
        assert connection.id == declined_id  # reused, not a new row

        rows = (await db_session.execute(select(UserConnection))).scalars().all()
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_declined_reverse_connection_lets_the_original_sender_try_again(self, db_session):
        """THE LIFECYCLE FIX, reverse direction: B requested A, A
        declined. B must still be able to send A a brand new request --
        the declined reverse row must not be silently returned as-is,
        and must not block a genuine new forward-direction INSERT."""
        a, b = _user(), _user()
        db_session.add_all([a, b])
        await db_session.flush()
        db_session.add(UserConnection(id=uuid.uuid4(), user_id=b.id, friend_id=a.id, status="declined"))
        await db_session.commit()

        connection = await social_service.request_friend(db_session, b.id, a.id)
        assert connection.status == "pending"
        assert connection.user_id == b.id
        assert connection.friend_id == a.id

    @pytest.mark.asyncio
    async def test_re_request_after_decline_notifies_the_recipient_again(self, db_session):
        """A re-opened (declined -> pending) request must actually
        re-notify, not silently update the row -- otherwise the
        recipient has no way to discover the new request exists."""
        a, b = _user(), _user()
        db_session.add_all([a, b])
        await db_session.flush()
        db_session.add(UserConnection(id=uuid.uuid4(), user_id=a.id, friend_id=b.id, status="declined"))
        await db_session.commit()

        await social_service.request_friend(db_session, a.id, b.id)

        rows = (await db_session.execute(
            text("SELECT user_id FROM notifications WHERE user_id = :uid"), {"uid": b.id.hex}
        )).all()
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_self_request_is_rejected(self, db_session):
        a = _user()
        db_session.add(a)
        await db_session.commit()

        with pytest.raises(social_service.SelfConnectionError):
            await social_service.request_friend(db_session, a.id, a.id)


class TestRespondToFriendRequestAuthorization:
    """IDOR guard on `respond_to_friend_request`: only the actual
    RECIPIENT (`friend_id` on the row) may accept/decline it -- not the
    sender, and not an unrelated third party who merely guesses or
    enumerates a `connection_id`."""

    @pytest.mark.asyncio
    async def test_sender_cannot_respond_to_their_own_outgoing_request(self, db_session):
        a, b = _user(), _user()
        db_session.add_all([a, b])
        await db_session.flush()
        connection_id = uuid.uuid4()
        db_session.add(UserConnection(id=connection_id, user_id=a.id, friend_id=b.id, status="pending"))
        await db_session.commit()

        with pytest.raises(social_service.NotAParticipantError):
            await social_service.respond_to_friend_request(db_session, a.id, connection_id, "accept")

    @pytest.mark.asyncio
    async def test_unrelated_third_party_cannot_respond_idor(self, db_session):
        """The actual IDOR case: a third user who is neither sender nor
        recipient, but has somehow obtained/guessed the `connection_id`
        (e.g. enumerating sequential/leaked ids), must be rejected."""
        a, b, mallory = _user(), _user(), _user()
        db_session.add_all([a, b, mallory])
        await db_session.flush()
        connection_id = uuid.uuid4()
        db_session.add(UserConnection(id=connection_id, user_id=a.id, friend_id=b.id, status="pending"))
        await db_session.commit()

        with pytest.raises(social_service.NotAParticipantError):
            await social_service.respond_to_friend_request(db_session, mallory.id, connection_id, "accept")

        # The row itself must be untouched by the rejected attempt.
        edge = await _single_edge(db_session, a.id, b.id)
        assert edge.status == "pending"

    @pytest.mark.asyncio
    async def test_responding_to_a_nonexistent_connection_id_raises_not_found(self, db_session):
        a = _user()
        db_session.add(a)
        await db_session.commit()

        with pytest.raises(social_service.ConnectionNotFoundError):
            await social_service.respond_to_friend_request(db_session, a.id, uuid.uuid4(), "accept")

    @pytest.mark.asyncio
    async def test_recipient_can_accept_their_own_incoming_request(self, db_session):
        a, b = _user(), _user()
        db_session.add_all([a, b])
        await db_session.flush()
        connection_id = uuid.uuid4()
        db_session.add(UserConnection(id=connection_id, user_id=a.id, friend_id=b.id, status="pending"))
        await db_session.commit()

        connection = await social_service.respond_to_friend_request(db_session, b.id, connection_id, "accept")
        assert connection.status == "accepted"

    @pytest.mark.asyncio
    async def test_responding_to_an_already_resolved_request_is_rejected(self, db_session):
        """Not a pending row anymore (already accepted) -- a second
        accept/decline call must be rejected as an invalid state
        transition, not silently re-applied."""
        a, b = _user(), _user()
        db_session.add_all([a, b])
        await db_session.flush()
        connection_id = uuid.uuid4()
        db_session.add(UserConnection(id=connection_id, user_id=a.id, friend_id=b.id, status="accepted"))
        await db_session.commit()

        with pytest.raises(social_service.InvalidStateError):
            await social_service.respond_to_friend_request(db_session, b.id, connection_id, "decline")
