"""Behavioral tests for Live 1:1 Video Sessions:
`app/services/live_session_service.py` (booking lifecycle state machine),
`app/api/v1/endpoints/live_sessions.py`'s REST endpoints (real HTTP status
codes via `TestClient`), and that same file's WebRTC signaling WebSocket
handler (auth -> authorize -> relay).

In-memory SQLite, `StaticPool`, same established discipline as
`tests/test_chat.py` (this feature's own closest sibling: a WebSocket
router with Redis Pub/Sub fan-out and real-JWT WS auth):

- `users` -- raw SQL (`User.skills` is a Postgres `ARRAY(String)` column;
  SQLite's DDL compiler has no `visit_ARRAY`).
- `user_bookmarks` -- raw SQL, empty/unused. Necessary plumbing only:
  `User.bookmarks` is `lazy="selectin"`, so any fresh `SELECT`/refresh of
  a `User` eagerly issues a second query against `user_bookmarks` --
  without the table present (even empty) that query fails with "no such
  table" (same finding already disclosed in `test_chat.py`/
  `test_courses_enrollment.py`).
- `live_sessions` -- created directly off the real ORM metadata via
  `Base.metadata.create_all(tables=[LiveSession.__table__])`. Unlike
  `lesson_study_aids`/`courses.embedding` elsewhere in this codebase,
  `LiveSession` has NO Postgres-specific column types (no `JSONB`,
  `ARRAY`, or pgvector `Vector`) -- every column is a plain
  String/Integer/DateTime/Text, and its two `CheckConstraint`s compile
  and are enforced natively by SQLite -- so no raw-SQL workaround is
  needed for this table specifically.

No real Redis server is reachable in this sandbox (same finding already
disclosed in `test_chat.py`). Unlike that file (which still exercises a
`_FakeRedis` stand-in for its separate ban-service tests),
`live_session_connection_manager.publish_signal` is monkeypatched
directly to call `manager.broadcast_local` -- no fake Redis client is
needed at all here, since this feature's only Redis touchpoint IS
`publish_signal` itself. This stands in for "a pub/sub listener task is
running somewhere and will re-deliver this," exactly what the real
listener itself does with a message it receives (same substitution
`test_chat.py`'s own `chat_client` fixture already establishes and
justifies for its own, structurally identical, `publish_message`).
"""

import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user
from app.core.config import settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.enums import LiveSessionStatus
from app.models.live_session import LiveSession
from app.models.user import User
from app.services import live_session_service

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------
# Shared DB fixture
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
    bio TEXT,
    search_vector TEXT,
    role VARCHAR(20) NOT NULL DEFAULT 'student',
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
        await conn.run_sync(Base.metadata.create_all, tables=[LiveSession.__table__])

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_session_factory):
    async with db_session_factory() as session:
        yield session


async def _make_user(db: AsyncSession, *, full_name: str = "Test User") -> User:
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.com", full_name=full_name)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


def _future(minutes: int = 60) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


# ---------------------------------------------------------------------
# Service-layer tests
# ---------------------------------------------------------------------


class TestLiveSessionServiceLifecycle:
    async def test_request_creates_a_requested_session(self, db_session):
        alice = await _make_user(db_session, full_name="Alice")
        bob = await _make_user(db_session, full_name="Bob")

        session = await live_session_service.request_session(
            db_session, alice.id, bob.id, _future(), 30, "Mock interview practice"
        )

        assert session.status == LiveSessionStatus.REQUESTED.value
        assert session.requested_by_id == alice.id
        assert session.requested_with_id == bob.id
        assert session.topic == "Mock interview practice"

    async def test_cannot_request_yourself(self, db_session):
        alice = await _make_user(db_session, full_name="Alice")

        with pytest.raises(live_session_service.SelfSessionError):
            await live_session_service.request_session(db_session, alice.id, alice.id, _future(), 30, None)

    async def test_nonexistent_counterparty_raises(self, db_session):
        alice = await _make_user(db_session, full_name="Alice")

        with pytest.raises(live_session_service.CounterpartyNotFoundError):
            await live_session_service.request_session(
                db_session, alice.id, uuid.uuid4(), _future(), 30, None
            )

    async def test_scheduled_at_in_the_past_raises(self, db_session):
        alice = await _make_user(db_session, full_name="Alice")
        bob = await _make_user(db_session, full_name="Bob")
        past = datetime.now(timezone.utc) - timedelta(minutes=5)

        with pytest.raises(live_session_service.InvalidTransitionError):
            await live_session_service.request_session(db_session, alice.id, bob.id, past, 30, None)

    async def test_invited_party_can_confirm(self, db_session):
        alice = await _make_user(db_session, full_name="Alice")
        bob = await _make_user(db_session, full_name="Bob")
        session = await live_session_service.request_session(db_session, alice.id, bob.id, _future(), 30, None)

        confirmed = await live_session_service.respond_to_session(
            db_session, session.id, bob.id, confirm=True
        )

        assert confirmed.status == LiveSessionStatus.CONFIRMED.value
        assert confirmed.responded_at is not None

    async def test_invited_party_can_decline(self, db_session):
        alice = await _make_user(db_session, full_name="Alice")
        bob = await _make_user(db_session, full_name="Bob")
        session = await live_session_service.request_session(db_session, alice.id, bob.id, _future(), 30, None)

        declined = await live_session_service.respond_to_session(
            db_session, session.id, bob.id, confirm=False
        )

        assert declined.status == LiveSessionStatus.DECLINED.value

    async def test_requester_cannot_respond_to_their_own_request(self, db_session):
        alice = await _make_user(db_session, full_name="Alice")
        bob = await _make_user(db_session, full_name="Bob")
        session = await live_session_service.request_session(db_session, alice.id, bob.id, _future(), 30, None)

        with pytest.raises(live_session_service.NotTheInvitedPartyError):
            await live_session_service.respond_to_session(db_session, session.id, alice.id, confirm=True)

    async def test_responding_twice_raises(self, db_session):
        alice = await _make_user(db_session, full_name="Alice")
        bob = await _make_user(db_session, full_name="Bob")
        session = await live_session_service.request_session(db_session, alice.id, bob.id, _future(), 30, None)
        await live_session_service.respond_to_session(db_session, session.id, bob.id, confirm=True)

        with pytest.raises(live_session_service.InvalidTransitionError):
            await live_session_service.respond_to_session(db_session, session.id, bob.id, confirm=True)

    async def test_either_party_can_cancel_a_requested_session(self, db_session):
        alice = await _make_user(db_session, full_name="Alice")
        bob = await _make_user(db_session, full_name="Bob")
        session = await live_session_service.request_session(db_session, alice.id, bob.id, _future(), 30, None)

        cancelled = await live_session_service.cancel_session(db_session, session.id, alice.id)

        assert cancelled.status == LiveSessionStatus.CANCELLED.value

    async def test_a_non_participant_cannot_cancel(self, db_session):
        alice = await _make_user(db_session, full_name="Alice")
        bob = await _make_user(db_session, full_name="Bob")
        carol = await _make_user(db_session, full_name="Carol")
        session = await live_session_service.request_session(db_session, alice.id, bob.id, _future(), 30, None)

        with pytest.raises(live_session_service.NotAParticipantError):
            await live_session_service.cancel_session(db_session, session.id, carol.id)

    async def test_cannot_cancel_an_already_completed_session(self, db_session):
        alice = await _make_user(db_session, full_name="Alice")
        bob = await _make_user(db_session, full_name="Bob")
        session = await live_session_service.request_session(db_session, alice.id, bob.id, _future(), 30, None)
        await live_session_service.respond_to_session(db_session, session.id, bob.id, confirm=True)
        await live_session_service.complete_session(db_session, session.id, alice.id, 5, "Great session")

        with pytest.raises(live_session_service.InvalidTransitionError):
            await live_session_service.cancel_session(db_session, session.id, alice.id)

    async def test_complete_requires_confirmed_status(self, db_session):
        alice = await _make_user(db_session, full_name="Alice")
        bob = await _make_user(db_session, full_name="Bob")
        session = await live_session_service.request_session(db_session, alice.id, bob.id, _future(), 30, None)

        with pytest.raises(live_session_service.InvalidTransitionError):
            await live_session_service.complete_session(db_session, session.id, alice.id, None, None)

    async def test_complete_persists_rating_and_feedback(self, db_session):
        alice = await _make_user(db_session, full_name="Alice")
        bob = await _make_user(db_session, full_name="Bob")
        session = await live_session_service.request_session(db_session, alice.id, bob.id, _future(), 30, None)
        await live_session_service.respond_to_session(db_session, session.id, bob.id, confirm=True)

        completed = await live_session_service.complete_session(
            db_session, session.id, bob.id, 4, "Helpful mentor call"
        )

        assert completed.status == LiveSessionStatus.COMPLETED.value
        assert completed.rating == 4
        assert completed.feedback == "Helpful mentor call"

    async def test_list_my_sessions_covers_both_directions(self, db_session):
        alice = await _make_user(db_session, full_name="Alice")
        bob = await _make_user(db_session, full_name="Bob")
        carol = await _make_user(db_session, full_name="Carol")

        await live_session_service.request_session(db_session, alice.id, bob.id, _future(), 30, None)
        await live_session_service.request_session(db_session, carol.id, alice.id, _future(90), 30, None)
        await live_session_service.request_session(db_session, bob.id, carol.id, _future(120), 30, None)

        alice_sessions = await live_session_service.list_my_sessions(db_session, alice.id)

        assert len(alice_sessions) == 2

    async def test_assert_can_join_requires_confirmed(self, db_session):
        alice = await _make_user(db_session, full_name="Alice")
        bob = await _make_user(db_session, full_name="Bob")
        session = await live_session_service.request_session(db_session, alice.id, bob.id, _future(), 30, None)

        with pytest.raises(live_session_service.InvalidTransitionError):
            await live_session_service.assert_can_join(db_session, session.id, alice.id)

        await live_session_service.respond_to_session(db_session, session.id, bob.id, confirm=True)
        joined = await live_session_service.assert_can_join(db_session, session.id, alice.id)
        assert joined.id == session.id

    async def test_assert_can_join_rejects_a_non_participant(self, db_session):
        alice = await _make_user(db_session, full_name="Alice")
        bob = await _make_user(db_session, full_name="Bob")
        carol = await _make_user(db_session, full_name="Carol")
        session = await live_session_service.request_session(db_session, alice.id, bob.id, _future(), 30, None)
        await live_session_service.respond_to_session(db_session, session.id, bob.id, confirm=True)

        with pytest.raises(live_session_service.NotAParticipantError):
            await live_session_service.assert_can_join(db_session, session.id, carol.id)


# ---------------------------------------------------------------------
# Router-level (real HTTP status code / response envelope) tests
# ---------------------------------------------------------------------


class TestLiveSessionRouterHTTP:
    @pytest.fixture
    def client_factory(self, db_session_factory):
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

    async def test_request_returns_201_with_other_party(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            alice = await _make_user(db, full_name="Alice")
            bob = await _make_user(db, full_name="Bob")

        client = client_factory(alice)
        response = client.post(
            "/api/v1/live-sessions/request",
            json={
                "requested_with_id": str(bob.id),
                "scheduled_at": _future().isoformat(),
                "duration_minutes": 45,
                "topic": "Portfolio review",
            },
        )

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "requested"
        assert body["other_party"]["full_name"] == "Bob"
        assert body["duration_minutes"] == 45

    async def test_request_with_self_returns_400(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            alice = await _make_user(db, full_name="Alice")

        client = client_factory(alice)
        response = client.post(
            "/api/v1/live-sessions/request",
            json={"requested_with_id": str(alice.id), "scheduled_at": _future().isoformat()},
        )

        assert response.status_code == 400

    async def test_only_invited_party_can_confirm(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            alice = await _make_user(db, full_name="Alice")
            bob = await _make_user(db, full_name="Bob")
            session = await live_session_service.request_session(db, alice.id, bob.id, _future(), 30, None)

        # Alice (the requester) trying to confirm her own request -- forbidden.
        client = client_factory(alice)
        response = client.post(f"/api/v1/live-sessions/{session.id}/respond", json={"action": "confirm"})
        assert response.status_code == 403

    async def test_confirm_then_complete_returns_200_with_rating(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            alice = await _make_user(db, full_name="Alice")
            bob = await _make_user(db, full_name="Bob")
            session = await live_session_service.request_session(db, alice.id, bob.id, _future(), 30, None)

        bob_client = client_factory(bob)
        confirm_response = bob_client.post(
            f"/api/v1/live-sessions/{session.id}/respond", json={"action": "confirm"}
        )
        assert confirm_response.status_code == 200
        assert confirm_response.json()["status"] == "confirmed"

        alice_client = client_factory(alice)
        complete_response = alice_client.post(
            f"/api/v1/live-sessions/{session.id}/complete", json={"rating": 5, "feedback": "Great!"}
        )
        assert complete_response.status_code == 200
        body = complete_response.json()
        assert body["status"] == "completed"
        assert body["rating"] == 5

    async def test_cancel_nonexistent_session_returns_404(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            alice = await _make_user(db, full_name="Alice")

        client = client_factory(alice)
        response = client.post(f"/api/v1/live-sessions/{uuid.uuid4()}/cancel")
        assert response.status_code == 404

    async def test_mine_returns_sessions_from_both_directions(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            central = await _make_user(db, full_name="Central")
            other_a = await _make_user(db, full_name="Other A")
            other_b = await _make_user(db, full_name="Other B")

            await live_session_service.request_session(db, central.id, other_a.id, _future(), 30, None)
            await live_session_service.request_session(db, other_b.id, central.id, _future(90), 30, None)

        client = client_factory(central)
        response = client.get("/api/v1/live-sessions/mine")

        assert response.status_code == 200
        assert len(response.json()) == 2

    async def test_ice_config_returns_the_configured_stun_server(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            alice = await _make_user(db, full_name="Alice")

        client = client_factory(alice)
        response = client.get("/api/v1/live-sessions/ice-config")

        assert response.status_code == 200
        servers = response.json()["ice_servers"]
        assert any("stun:" in server["urls"] for server in servers)


# ---------------------------------------------------------------------
# WebSocket-layer tests (signaling relay)
# ---------------------------------------------------------------------


def _mint_token(user_id: uuid.UUID, email: str | None = "student@example.com") -> str:
    payload = {
        "sub": str(user_id),
        "email": email,
        "aud": "authenticated",
        "exp": datetime.now(timezone.utc).timestamp() + 3600,
    }
    return jwt.encode(payload, settings.supabase_jwt_secret, algorithm=settings.jwt_algorithm)


@pytest.fixture(autouse=True)
def _test_supabase_jwt_secret(monkeypatch):
    """Same reasoning as `test_chat.py`'s own fixture of this exact name
    -- this sandbox's `.env` has an empty `SUPABASE_JWT_SECRET`, which
    `jwt.encode` refuses outright; a real non-empty secret is needed to
    mint and verify a genuine signed JWT for the WebSocket auth boundary
    (which, unlike `get_current_user`, cannot be bypassed via
    `app.dependency_overrides` -- see `app/api/deps.py`'s
    `get_websocket_user` docstring)."""
    monkeypatch.setattr(settings, "supabase_jwt_secret", "test-only-signing-secret-do-not-use-in-prod")


@pytest.fixture
def live_session_client(db_session_factory, monkeypatch):
    import app.services.live_session_connection_manager as lscm

    async def _broadcast_directly(session_id, payload):
        await lscm.manager.broadcast_local(session_id, payload)

    monkeypatch.setattr(lscm, "publish_signal", _broadcast_directly)
    # Same "the router module imported the name directly, so the SOURCE
    # module's attribute must ALSO be patched on the router module
    # itself" fix already established by `test_chat.py`'s own
    # `chat_client` fixture.
    import app.api.v1.endpoints.live_sessions as live_sessions_router_module

    monkeypatch.setattr(live_sessions_router_module, "publish_signal", _broadcast_directly)

    async def _override_get_db():
        async with db_session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestLiveSessionSignalingWebSocket:
    pytestmark = pytest.mark.asyncio

    async def test_rejects_a_connection_with_no_token(self, live_session_client):
        with live_session_client.websocket_connect(f"/api/v1/live-sessions/ws/{uuid.uuid4()}") as ws:
            event = ws.receive()
            assert event["type"] == "websocket.close"
            assert event["code"] == 4401

    async def test_rejects_a_nonexistent_session(self, db_session_factory, live_session_client):
        async with db_session_factory() as db:
            alice = await _make_user(db, full_name="Alice")

        token = _mint_token(alice.id)
        with live_session_client.websocket_connect(
            f"/api/v1/live-sessions/ws/{uuid.uuid4()}?token={token}"
        ) as ws:
            close = ws.receive()
            assert close["code"] == 4404

    async def test_rejects_a_non_participant(self, db_session_factory, live_session_client):
        async with db_session_factory() as db:
            alice = await _make_user(db, full_name="Alice")
            bob = await _make_user(db, full_name="Bob")
            carol = await _make_user(db, full_name="Carol")
            session = await live_session_service.request_session(db, alice.id, bob.id, _future(), 30, None)
            await live_session_service.respond_to_session(db, session.id, bob.id, confirm=True)

        token = _mint_token(carol.id)
        with live_session_client.websocket_connect(
            f"/api/v1/live-sessions/ws/{session.id}?token={token}"
        ) as ws:
            close = ws.receive()
            assert close["code"] == 4403

    async def test_rejects_a_not_yet_confirmed_session(self, db_session_factory, live_session_client):
        async with db_session_factory() as db:
            alice = await _make_user(db, full_name="Alice")
            bob = await _make_user(db, full_name="Bob")
            session = await live_session_service.request_session(db, alice.id, bob.id, _future(), 30, None)

        token = _mint_token(alice.id)
        with live_session_client.websocket_connect(
            f"/api/v1/live-sessions/ws/{session.id}?token={token}"
        ) as ws:
            close = ws.receive()
            assert close["code"] == 4409

    async def test_an_offer_is_relayed_to_both_connected_participants(
        self, db_session_factory, live_session_client
    ):
        async with db_session_factory() as db:
            alice = await _make_user(db, full_name="Alice")
            bob = await _make_user(db, full_name="Bob")
            session = await live_session_service.request_session(db, alice.id, bob.id, _future(), 30, None)
            await live_session_service.respond_to_session(db, session.id, bob.id, confirm=True)

        alice_token = _mint_token(alice.id)
        bob_token = _mint_token(bob.id)

        with live_session_client.websocket_connect(
            f"/api/v1/live-sessions/ws/{session.id}?token={alice_token}"
        ) as alice_ws, live_session_client.websocket_connect(
            f"/api/v1/live-sessions/ws/{session.id}?token={bob_token}"
        ) as bob_ws:
            alice_ws.send_text('{"type": "offer", "data": {"sdp": "v=0..."}}')

            alice_received = alice_ws.receive_json()
            bob_received = bob_ws.receive_json()

        # Broadcast reaches BOTH connected sockets, including the sender
        # itself -- same posture as `test_chat.py`'s own broadcast test;
        # the frontend's `useLiveCall` hook filters out its own echoed
        # `sender_id` rather than the backend suppressing it.
        assert alice_received == bob_received
        assert alice_received["type"] == "offer"
        assert alice_received["data"] == {"sdp": "v=0..."}
        assert alice_received["sender_id"] == str(alice.id)
