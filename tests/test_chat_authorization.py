"""P0-B (Integration Recovery Phase 1) regression coverage: `POST
/chat/rooms` for a DM (`is_group=False`) must require an ACCEPTED
`UserConnection` between the caller and the target, enforced at the
router (`app/api/v1/endpoints/chat.py::create_room`), BEFORE
`chat_service.get_or_create_dm_room` is ever called.

No HTTP-level test existed for this endpoint before this file --
confirmed by a repo-wide grep during this phase's own policy
reconstruction. `tests/test_chat.py::TestChatServicePersistence`
exercises `chat_service.get_or_create_dm_room`/`create_group_room`
directly (service layer, no router, no authorization) and is
deliberately left untouched by this fix -- the new connection check
lives only in the router, matching this codebase's own "router does
authorization, service stays reusable" convention.

Same hand-rolled minimal-SQLite-schema convention as
`tests/test_social_lifecycle.py` for `users`/`user_connections`
(Postgres-specific `User` columns -- `ARRAY`, `TSVECTOR` -- don't exist
on SQLite) and the same `client_factory` (get_db + get_current_user
dependency-override) convention as
`tests/test_live_sessions.py::TestLiveSessionRouterHTTP` for exercising
a real router through a real `TestClient`. `ChatRoom`/`ChatParticipant`/
`ChatMessage` have no Postgres-specific column types, so those three
are created via the real `Base.metadata.create_all(tables=[...])`
rather than hand-rolled SQL -- no reason to duplicate their schema by
hand when the ORM's own `create_all` works for them directly.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.chat import ChatParticipant, ChatRoom
from app.models.user import User
from app.services import chat_service

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

# `User.bookmarks` is `lazy="selectin"` -- every ORM load of a `User` row
# issues an automatic follow-up SELECT against this table (same fixture
# trap documented in `test_privacy.py`/`test_social_lifecycle.py`).
_CREATE_USER_BOOKMARKS_SQL = """
CREATE TABLE user_bookmarks (
    id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36) NOT NULL, opportunity_id VARCHAR(36),
    course_id VARCHAR(36), status VARCHAR(20) NOT NULL DEFAULT 'saved',
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


@pytest_asyncio.fixture
async def db_session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.execute(text(_CREATE_USERS_SQL))
        await conn.execute(text(_CREATE_USER_BOOKMARKS_SQL))
        await conn.execute(text(_CREATE_USER_CONNECTIONS_SQL))
        await conn.run_sync(Base.metadata.create_all, tables=[ChatRoom.__table__, ChatParticipant.__table__])

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


async def _make_user(db: AsyncSession, *, full_name: str = "Test User") -> User:
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex}@example.com", full_name=full_name)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_connection(db: AsyncSession, *, requester_id: uuid.UUID, recipient_id: uuid.UUID, status: str) -> None:
    """Direct, minimal `UserConnection` row insert -- deliberately NOT
    routed through `social_service.request_friend`/`respond_to_friend_
    request` (those are already covered by `test_social_lifecycle.py`;
    this file only needs a real row in a known state, not re-proof of
    the lifecycle that produces it)."""
    await db.execute(
        text(
            "INSERT INTO user_connections (id, user_id, friend_id, status) "
            "VALUES (:id, :user_id, :friend_id, :status)"
        ),
        {"id": uuid.uuid4().hex, "user_id": requester_id.hex, "friend_id": recipient_id.hex, "status": status},
    )
    await db.commit()


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


@pytest.mark.asyncio
class TestChatRoomDMAuthorization:
    async def test_connected_user_can_create_dm(self, db_session_factory, client_factory):
        """A. CONNECTED USER -- request succeeds, a real room is created,
        and the real service function is actually reached (not just that
        the HTTP call happens to return 201)."""
        async with db_session_factory() as db:
            alice = await _make_user(db, full_name="Alice")
            bob = await _make_user(db, full_name="Bob")
            await _make_connection(db, requester_id=alice.id, recipient_id=bob.id, status="accepted")

        client = client_factory(alice)
        response = client.post("/api/v1/chat/rooms", json={"is_group": False, "participant_ids": [str(bob.id)]})

        assert response.status_code == 201
        body = response.json()
        assert body["is_group"] is False
        participant_ids = {p["id"] for p in body["participants"]}
        assert participant_ids == {str(alice.id), str(bob.id)}

        async with db_session_factory() as db:
            rooms = (await db.execute(select(ChatRoom))).scalars().all()
            assert len(rooms) == 1

    async def test_no_connection_is_rejected(self, db_session_factory, client_factory):
        """B. NO CONNECTION -- 403, no room created, and the real
        service function is proven NOT invoked (not merely that no room
        exists afterward, which an early-return inside the service could
        also produce) -- monkeypatched to raise if called at all."""
        async with db_session_factory() as db:
            alice = await _make_user(db, full_name="Alice")
            stranger = await _make_user(db, full_name="Stranger")

        client = client_factory(alice)
        original = chat_service.get_or_create_dm_room
        chat_service.get_or_create_dm_room = AsyncMock(side_effect=AssertionError("must not be called"))
        try:
            response = client.post(
                "/api/v1/chat/rooms", json={"is_group": False, "participant_ids": [str(stranger.id)]}
            )
        finally:
            chat_service.get_or_create_dm_room = original

        assert response.status_code == 403
        async with db_session_factory() as db:
            rooms = (await db.execute(select(ChatRoom))).scalars().all()
            assert rooms == []

    async def test_request_sent_is_rejected(self, db_session_factory, client_factory):
        """C. REQUEST SENT -- caller already sent a pending request to
        the target; still not a connection, must still be 403."""
        async with db_session_factory() as db:
            alice = await _make_user(db, full_name="Alice")
            bob = await _make_user(db, full_name="Bob")
            await _make_connection(db, requester_id=alice.id, recipient_id=bob.id, status="pending")

        client = client_factory(alice)
        response = client.post("/api/v1/chat/rooms", json={"is_group": False, "participant_ids": [str(bob.id)]})

        assert response.status_code == 403
        async with db_session_factory() as db:
            rooms = (await db.execute(select(ChatRoom))).scalars().all()
            assert rooms == []

    async def test_request_received_is_rejected(self, db_session_factory, client_factory):
        """D. REQUEST RECEIVED -- the target sent the caller a pending
        request; still not an accepted connection, must still be 403 --
        a pending request in EITHER direction is not enough to message."""
        async with db_session_factory() as db:
            alice = await _make_user(db, full_name="Alice")
            bob = await _make_user(db, full_name="Bob")
            await _make_connection(db, requester_id=bob.id, recipient_id=alice.id, status="pending")

        client = client_factory(alice)
        response = client.post("/api/v1/chat/rooms", json={"is_group": False, "participant_ids": [str(bob.id)]})

        assert response.status_code == 403
        async with db_session_factory() as db:
            rooms = (await db.execute(select(ChatRoom))).scalars().all()
            assert rooms == []

    async def test_self_dm_keeps_its_existing_422_not_a_new_403(self, db_session_factory, client_factory):
        """E. SELF -- the pre-existing `SelfDMError` -> 422 behavior must
        be preserved exactly, not silently replaced by the new
        connection-based 403 (`get_connection_state` would itself return
        "self", which is != "connected" -- this proves the self case is
        short-circuited BEFORE that check runs)."""
        async with db_session_factory() as db:
            alice = await _make_user(db, full_name="Alice")

        client = client_factory(alice)
        response = client.post("/api/v1/chat/rooms", json={"is_group": False, "participant_ids": [str(alice.id)]})

        assert response.status_code == 422
        assert "yourself" in response.json()["detail"].lower()

    async def test_group_chat_is_unaffected_by_dm_authorization(self, db_session_factory, client_factory):
        """G. GROUP CHAT -- `is_group=True` must NOT be gated by the new
        connection check at all, even for completely unconnected members
        -- no equivalent policy exists for groups (out of scope), and
        `create_group_room`'s own pre-existing behavior must be
        unchanged."""
        async with db_session_factory() as db:
            alice = await _make_user(db, full_name="Alice")
            stranger_one = await _make_user(db, full_name="Stranger One")
            stranger_two = await _make_user(db, full_name="Stranger Two")

        client = client_factory(alice)
        response = client.post(
            "/api/v1/chat/rooms",
            json={
                "is_group": True,
                "name": "Study Squad",
                "participant_ids": [str(stranger_one.id), str(stranger_two.id)],
            },
        )

        assert response.status_code == 201
        body = response.json()
        assert body["is_group"] is True
        participant_ids = {p["id"] for p in body["participants"]}
        assert participant_ids == {str(alice.id), str(stranger_one.id), str(stranger_two.id)}


class TestChatRoomDMAuthorizationUnauthenticated:
    def test_unauthenticated_request_is_rejected(self):
        """F. UNAUTHENTICATED -- no dependency override at all; the
        existing authentication requirement must still apply unchanged.
        Deliberately its own, non-async-marked class -- this is a plain
        sync test (`TestClient` requests are synchronous by design), and
        `TestChatRoomDMAuthorization` above is class-marked
        `@pytest.mark.asyncio` for its own (genuinely async) tests; mixing
        the two in one class produces a real `PytestWarning` (caught and
        fixed, not shipped) -- same posture `test_chat.py`'s own module
        docstring documents for the identical situation."""
        app.dependency_overrides.clear()
        client = TestClient(app)
        response = client.post(
            "/api/v1/chat/rooms", json={"is_group": False, "participant_ids": [str(uuid.uuid4())]}
        )
        assert response.status_code in (401, 403)
