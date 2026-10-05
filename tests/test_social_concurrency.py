"""Final Hardening Gate, Sections 6-9: real concurrency testing of the
state-changing social-graph operations (`request_friend`,
`respond_to_friend_request`) -- run as genuinely interleaved `asyncio`
tasks via `asyncio.gather`, not two sequential calls (which proves
nothing about concurrency safety, only about idempotency).

TEST INFRASTRUCTURE NOTE: this project has no live Postgres in its test
sandbox (same standing limitation as `test_user_search.py`/`test_
privacy.py`). An EARLIER version of this file used the rest of this
suite's usual in-memory-SQLite-plus-`StaticPool` convention (one
physical connection shared by every session, which is how that
convention makes an in-memory DB visible across sessions at all) --
that turned out to be the wrong tool for THIS specific job: sharing one
raw DBAPI connection between two concurrently-running `AsyncSession`s is
not a valid way to model two real, independently-pooled database
connections, and it produced a reproducible false failure (a bare
`InvalidRequestError` on the winning call's own post-commit `refresh()`,
and the committed row disappearing entirely) that had nothing to do
with `request_friend`'s actual logic -- confirmed by re-running the
identical scenario against a real file-based SQLite database (genuinely
separate connections per session, real OS-level file locking) five
times in a row: every single run produced the correct, clean result
(exactly one success, one clean `DuplicateConnectionError`, exactly one
persisted row). This file now uses a temp-file-based SQLite database
for that reason -- still not Postgres, but with real per-session
connections, which is the property that actually matters for testing
concurrent access.

REAL BUG FOUND AND FIXED BY THIS FILE: running cases B and C against
the corrected engine did NOT just surface a test-infrastructure
artifact -- it proved `respond_to_friend_request`'s old `SELECT ... FOR
UPDATE`-then-UPDATE implementation has a genuine, silent LOST UPDATE
under SQLite (both concurrent accept calls observed `status="pending"`
and both wrote with no exception at all), because SQLAlchemy's SQLite
dialect never emits a locking clause for `with_for_update()` -- it is a
silent no-op. `respond_to_friend_request` was rewritten to use a single
atomic conditional `UPDATE ... WHERE status = 'pending'` and check its
matched-row count instead, which is correct on SQLite AND Postgres
without depending on `FOR UPDATE` support at all. Re-running cases B
and C after that fix confirms exactly one call now wins and the other
now cleanly raises `InvalidStateError`, every time -- see each test's
own docstring below. A live-Postgres load test under real lock
contention remains a disclosed, unreached gap -- see the final report's
Known Limitations.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, friend_id)
)
"""

_CREATE_NOTIFICATIONS_SQL = """
CREATE TABLE notifications (
    id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36) NOT NULL, type VARCHAR(50) NOT NULL,
    title VARCHAR(255) NOT NULL, message TEXT NOT NULL, payload TEXT,
    is_read BOOLEAN NOT NULL DEFAULT 0, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


@pytest_asyncio.fixture
async def engine():
    """A real temp-file SQLite database, NOT `StaticPool`-shared
    in-memory -- see the module docstring for why this test specifically
    needs genuinely separate per-session connections."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    eng = create_async_engine(f"sqlite+aiosqlite:///{path}", connect_args={"timeout": 5})
    async with eng.begin() as conn:
        await conn.execute(text(_CREATE_USERS_SQL))
        await conn.execute(text(_CREATE_USER_CONNECTIONS_SQL))
        await conn.execute(text(_CREATE_NOTIFICATIONS_SQL))
    yield eng
    await eng.dispose()
    if os.path.exists(path):
        os.remove(path)


def _make_user(**overrides) -> User:
    defaults = dict(id=uuid.uuid4(), email=f"{uuid.uuid4().hex}@example.com", full_name="User")
    defaults.update(overrides)
    return User(**defaults)


async def _new_session(engine) -> AsyncSession:
    """Each concurrent task gets its OWN session/connection-from-the-
    pool, matching how two real concurrent HTTP requests would each get
    their own session from `get_db` -- sharing one session across
    `asyncio.gather` tasks would not exercise the same code path a real
    concurrent request takes."""
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return factory()


class TestFriendRequestConcurrency:
    @pytest.mark.asyncio
    async def test_case_a_duplicate_simultaneous_requests_produce_exactly_one_connection(self, engine):
        """A: User A sends two connection requests to User B at the same
        time. Expected: no duplicate logical request survives.

        Against the temp-file-based SQLite engine (genuinely separate
        per-session connections, real OS-level file locking -- see the
        module docstring for why that replaced the earlier `StaticPool`
        in-memory setup), this is deterministic and clean, confirmed by
        direct reproduction and by this test's own run: exactly one
        concurrent call succeeds and returns the new `UserConnection`
        (status "pending"), and the other cleanly raises
        `DuplicateConnectionError` from the `(user_id, friend_id)`
        UNIQUE-constraint `IntegrityError` that `request_friend` catches
        -- the same failure mode expected on real Postgres. No
        `InvalidRequestError`/refresh artifact occurs once sessions are
        genuinely independent."""
        setup = await _new_session(engine)
        user_a, user_b = _make_user(), _make_user()
        setup.add_all([user_a, user_b])
        await setup.commit()
        await setup.close()

        session_1 = await _new_session(engine)
        session_2 = await _new_session(engine)
        results = await asyncio.gather(
            social_service.request_friend(session_1, user_a.id, user_b.id),
            social_service.request_friend(session_2, user_a.id, user_b.id),
            return_exceptions=True,
        )
        await session_1.close()
        await session_2.close()

        succeeded = [r for r in results if not isinstance(r, Exception)]
        failed = [r for r in results if isinstance(r, Exception)]
        assert len(succeeded) == 1  # exactly one call wins the race
        assert len(failed) == 1  # exactly one call loses it, no silent double-loss
        assert isinstance(failed[0], social_service.DuplicateConnectionError)
        assert succeeded[0].status == "pending"

        verify = await _new_session(engine)
        rows = (await verify.execute(
            select(UserConnection).where(UserConnection.user_id == user_a.id, UserConnection.friend_id == user_b.id)
        )).scalars().all()
        assert len(rows) == 1  # the invariant that actually matters
        assert rows[0].status == "pending"
        await verify.close()

    @pytest.mark.asyncio
    async def test_case_b_double_accept_produces_one_terminal_state(self, engine):
        """B: User B accepts the same request twice concurrently.
        Expected: exactly one call succeeds, the other is rejected --
        never both silently succeeding, never a corrupted state.

        This test originally surfaced a REAL bug: the previous
        `respond_to_friend_request` implementation relied on `SELECT
        ... FOR UPDATE`, which SQLite's SQLAlchemy dialect silently
        no-ops, so both concurrent calls observed `status="pending"`
        and both wrote with no exception -- a true LOST UPDATE (see the
        module docstring). `respond_to_friend_request` was rewritten to
        use a single atomic conditional UPDATE keyed on
        `status == "pending"`, which does not depend on `FOR UPDATE`
        support. Re-run against that fix, this is now deterministic:
        exactly one call wins and the other cleanly raises
        `InvalidStateError`, confirmed by direct reproduction and
        asserted strictly below."""
        setup = await _new_session(engine)
        user_a, user_b = _make_user(), _make_user()
        setup.add_all([user_a, user_b])
        await setup.flush()
        connection = UserConnection(id=uuid.uuid4(), user_id=user_a.id, friend_id=user_b.id, status="pending")
        setup.add(connection)
        await setup.commit()
        connection_id = connection.id
        await setup.close()

        session_1 = await _new_session(engine)
        session_2 = await _new_session(engine)
        results = await asyncio.gather(
            social_service.respond_to_friend_request(session_1, user_b.id, connection_id, "accept"),
            social_service.respond_to_friend_request(session_2, user_b.id, connection_id, "accept"),
            return_exceptions=True,
        )
        await session_1.close()
        await session_2.close()

        succeeded = [r for r in results if not isinstance(r, Exception)]
        failed = [r for r in results if isinstance(r, Exception)]
        assert len(succeeded) == 1  # exactly one call wins
        assert len(failed) == 1  # exactly one call loses, no silent double-success
        assert isinstance(failed[0], social_service.InvalidStateError)
        assert succeeded[0].status == "accepted"

        verify = await _new_session(engine)
        final = await verify.get(UserConnection, connection_id)
        assert final.status == "accepted"  # no corrupted third state
        await verify.close()

    @pytest.mark.asyncio
    async def test_case_c_concurrent_accept_and_decline_yields_one_valid_terminal_state(self, engine):
        """C: User B accepts and declines the same request concurrently.
        Expected: exactly one action wins, the other is rejected, and
        the final state is whichever one action actually won -- never
        both applying (a lost update) and never a corrupted state.

        Same underlying fix as case B: before `respond_to_friend_
        request`'s atomic-conditional-UPDATE rewrite, this raced to a
        "last write wins" lost update with neither call raising.
        Re-run against the fix, this is deterministic: whichever call's
        UPDATE statement actually executes first wins outright (its
        `status == "pending"` WHERE clause still matches), and the
        other always raises `InvalidStateError` -- confirmed by direct
        reproduction and asserted strictly below."""
        setup = await _new_session(engine)
        user_a, user_b = _make_user(), _make_user()
        setup.add_all([user_a, user_b])
        await setup.flush()
        connection = UserConnection(id=uuid.uuid4(), user_id=user_a.id, friend_id=user_b.id, status="pending")
        setup.add(connection)
        await setup.commit()
        connection_id = connection.id
        await setup.close()

        session_1 = await _new_session(engine)
        session_2 = await _new_session(engine)
        results = await asyncio.gather(
            social_service.respond_to_friend_request(session_1, user_b.id, connection_id, "accept"),
            social_service.respond_to_friend_request(session_2, user_b.id, connection_id, "decline"),
            return_exceptions=True,
        )
        await session_1.close()
        await session_2.close()

        succeeded = [r for r in results if not isinstance(r, Exception)]
        failed = [r for r in results if isinstance(r, Exception)]
        assert len(succeeded) == 1  # exactly one of accept/decline wins
        assert len(failed) == 1  # the other is rejected, not silently lost
        assert isinstance(failed[0], social_service.InvalidStateError)
        assert succeeded[0].status in ("accepted", "declined")

        verify = await _new_session(engine)
        final = await verify.get(UserConnection, connection_id)
        assert final.status in ("accepted", "declined")  # never a third, corrupted value
        assert final.status == succeeded[0].status  # the winner's write is what actually persisted
        await verify.close()

    @pytest.mark.asyncio
    async def test_case_d_reciprocal_requests_never_produce_a_corrupt_or_duplicate_graph(self, engine):
        """D: Two users send each other a friend request at roughly the
        same time. `request_friend`'s existing mutual-request-resolution
        logic (checking for the reverse row) is exercised under real
        concurrency here, rather than assumed safe from reading the code
        alone. Expected: no duplicate/corrupt state -- exactly one or
        two rows total, and every row's status is one of the real,
        valid enum values (never left in some other, invalid shape)."""
        setup = await _new_session(engine)
        user_a, user_b = _make_user(), _make_user()
        setup.add_all([user_a, user_b])
        await setup.commit()
        await setup.close()

        session_1 = await _new_session(engine)
        session_2 = await _new_session(engine)
        results = await asyncio.gather(
            social_service.request_friend(session_1, user_a.id, user_b.id),
            social_service.request_friend(session_2, user_b.id, user_a.id),
            return_exceptions=True,
        )
        await session_1.close()
        await session_2.close()

        # Neither direction should raise SelfConnectionError/unexpected
        # errors -- these are two distinct users, a legitimate reciprocal
        # pair. A DuplicateConnectionError here is an acceptable outcome
        # of the race (one direction's insert may collide with the
        # other's auto-accept-then-implicit-existing-row), but nothing
        # else should surface.
        for r in results:
            if isinstance(r, Exception):
                assert isinstance(r, social_service.DuplicateConnectionError)

        verify = await _new_session(engine)
        rows = (await verify.execute(select(UserConnection))).scalars().all()
        assert 1 <= len(rows) <= 2
        for row in rows:
            assert row.status in ("pending", "accepted", "declined")
        # No dangling half-written row: every row must actually name both users.
        for row in rows:
            assert {row.user_id, row.friend_id} == {user_a.id, user_b.id}
        await verify.close()
