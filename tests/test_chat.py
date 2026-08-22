"""Behavioral tests for the English-Only Gamified Real-time Chat step:
`app/services/chat_security.py` (Arabic filter + Redis ban service),
`app/services/chat_connection_manager.py` (in-process broadcast
registry), `app/services/chat_service.py` (room/message persistence),
and `app/api/v1/endpoints/chat.py`'s WebSocket handler (the full
authenticate -> ban-check -> filter -> save-and-broadcast flow).

No real Redis server is reachable in this sandbox (confirmed directly --
`redis://localhost:6379` refuses the connection). Every Redis-touching
function is exercised against `_FakeRedis`, an in-memory stand-in
implementing exactly the handful of commands this codebase's own Redis
usage needs (`set`/`exists`/`ttl`/`publish`) -- patched in at the exact
same `_get_redis_client()` boundary `chat_security.py`/
`chat_connection_manager.py` themselves already isolate for this purpose
(see both modules' own docstrings). This is the same "mock the one
external I/O boundary, let everything else run for real" posture as
every other externally-dependent service tested elsewhere in this
codebase (Gemini calls, R2/boto3, etc.) -- `contains_arabic`, the
`ConnectionManager`, `chat_service`'s SQL, and the WebSocket router's own
control flow all run for real.

WebSocket auth (`app/api/deps.py`'s `get_websocket_user`) is exercised
with REAL, validly-signed synthetic JWTs (`jwt.encode(..., settings.
supabase_jwt_secret, algorithm=settings.jwt_algorithm)`) rather than
mocked -- it's a plain function call inside the WS handler, not a
`Depends(...)`-injected parameter, so `app.dependency_overrides` cannot
intercept it the way `get_db` is overridden below; minting a real token
against this process's own configured secret is the correct way to
exercise it as a genuine boundary rather than a bypassed one.
"""

import uuid
from datetime import datetime, timezone

import jwt
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.session import get_db
from app.main import app
from app.models.chat import ChatMessage
from app.models.user import User
from app.services import chat_security, chat_service
from app.services.chat_connection_manager import ConnectionManager

# Deliberately NOT a module-level `pytestmark = pytest.mark.asyncio` --
# `TestArabicFilter` and `TestConnectionManager`'s internal-state
# assertion are plain sync tests; a module-level marker would tag them
# too, which pytest-asyncio warns about. Same established fix as
# `tests/test_aggregator.py`/`tests/test_security_shield.py`: apply
# `pytestmark = pytest.mark.asyncio` as a CLASS attribute only on
# classes whose tests are ALL async.

# ---------------------------------------------------------------------
# Component 1: the Arabic filter -- pure, no I/O.
# ---------------------------------------------------------------------


class TestArabicFilter:
    def test_plain_english_is_not_flagged(self):
        assert chat_security.contains_arabic("Hey, are we still on for the study group at 5?") is False

    def test_arabic_text_is_flagged(self):
        assert chat_security.contains_arabic("مرحبا كيف حالك") is True

    def test_mixed_english_and_arabic_is_flagged(self):
        assert chat_security.contains_arabic("Hello مرحبا there") is True

    def test_empty_string_is_not_flagged(self):
        assert chat_security.contains_arabic("") is False

    def test_numbers_and_punctuation_are_not_flagged(self):
        assert chat_security.contains_arabic("Meet @ 5:30pm? Room #204 -- bring notes!!") is False

    def test_a_single_arabic_character_is_enough(self):
        assert chat_security.contains_arabic("almost all english but one ا character") is True


# ---------------------------------------------------------------------
# Fake Redis -- shared by the ban-service tests and the WS integration
# tests below. Implements only what this codebase's Redis usage needs.
# ---------------------------------------------------------------------


class _FakeRedis:
    def __init__(self) -> None:
        self._store: dict[str, tuple[str, int | None]] = {}
        self.published: list[tuple[str, str]] = []

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._store[key] = (value, ex)

    async def exists(self, key: str) -> int:
        return 1 if key in self._store else 0

    async def ttl(self, key: str) -> int:
        if key not in self._store:
            return -2
        _, ex = self._store[key]
        return ex if ex is not None else -1

    def expire_now(self, key: str) -> None:
        """Test-only helper simulating a ban's TTL having already lapsed
        -- Redis itself deletes an expired key, so this reproduces that
        exact end state rather than actually waiting out a real TTL."""
        self._store.pop(key, None)

    async def publish(self, channel: str, message: str) -> None:
        self.published.append((channel, message))


@pytest.fixture
def fake_redis(monkeypatch):
    redis = _FakeRedis()
    monkeypatch.setattr(chat_security, "_get_redis_client", lambda: redis)
    return redis


# ---------------------------------------------------------------------
# Component 2: the Redis ban service.
# ---------------------------------------------------------------------


class TestBanService:
    pytestmark = pytest.mark.asyncio

    async def test_user_is_not_banned_by_default(self, fake_redis):
        user_id = uuid.uuid4()
        assert await chat_security.is_user_banned(user_id) is False
        assert await chat_security.get_ban_remaining_seconds(user_id) is None

    async def test_ban_user_sets_a_ttl_backed_ban(self, fake_redis):
        user_id = uuid.uuid4()
        await chat_security.ban_user(user_id, duration_seconds=1800)

        assert await chat_security.is_user_banned(user_id) is True
        assert await chat_security.get_ban_remaining_seconds(user_id) == 1800

    async def test_ban_is_scoped_to_one_user(self, fake_redis):
        banned_user = uuid.uuid4()
        other_user = uuid.uuid4()
        await chat_security.ban_user(banned_user)

        assert await chat_security.is_user_banned(banned_user) is True
        assert await chat_security.is_user_banned(other_user) is False

    async def test_expired_ban_reads_back_as_not_banned(self, fake_redis):
        """Redis deletes an expired key itself -- `is_user_banned`/
        `get_ban_remaining_seconds` need no extra bookkeeping to reflect
        that once it's happened."""
        user_id = uuid.uuid4()
        await chat_security.ban_user(user_id, duration_seconds=1800)
        fake_redis.expire_now(f"chat:ban:{user_id}")

        assert await chat_security.is_user_banned(user_id) is False
        assert await chat_security.get_ban_remaining_seconds(user_id) is None

    async def test_default_duration_is_thirty_minutes(self, fake_redis):
        user_id = uuid.uuid4()
        await chat_security.ban_user(user_id)
        assert await chat_security.get_ban_remaining_seconds(user_id) == 1800


# ---------------------------------------------------------------------
# Component 3: ConnectionManager (pure, in-memory -- fake socket
# objects, no real network I/O).
# ---------------------------------------------------------------------


class _FakeSocket:
    def __init__(self, *, fails: bool = False) -> None:
        self.sent: list[dict] = []
        self._fails = fails

    async def send_json(self, payload: dict) -> None:
        if self._fails:
            raise RuntimeError("socket is dead")
        self.sent.append(payload)


class TestConnectionManager:
    pytestmark = pytest.mark.asyncio

    async def test_broadcast_delivers_to_every_local_socket_in_the_room(self):
        manager = ConnectionManager()
        room_id = uuid.uuid4()
        socket_a, socket_b = _FakeSocket(), _FakeSocket()
        manager.connect(room_id, socket_a)
        manager.connect(room_id, socket_b)

        await manager.broadcast_local(room_id, {"type": "message", "content": "hi"})

        assert socket_a.sent == [{"type": "message", "content": "hi"}]
        assert socket_b.sent == [{"type": "message", "content": "hi"}]

    async def test_broadcast_does_not_leak_across_rooms(self):
        manager = ConnectionManager()
        room_a, room_b = uuid.uuid4(), uuid.uuid4()
        socket_a, socket_b = _FakeSocket(), _FakeSocket()
        manager.connect(room_a, socket_a)
        manager.connect(room_b, socket_b)

        await manager.broadcast_local(room_a, {"type": "message"})

        assert socket_a.sent == [{"type": "message"}]
        assert socket_b.sent == []

    async def test_exclude_skips_the_senders_own_socket(self):
        manager = ConnectionManager()
        room_id = uuid.uuid4()
        sender, other = _FakeSocket(), _FakeSocket()
        manager.connect(room_id, sender)
        manager.connect(room_id, other)

        await manager.broadcast_local(room_id, {"type": "message"}, exclude=sender)

        assert sender.sent == []
        assert other.sent == [{"type": "message"}]

    async def test_a_dead_socket_is_dropped_without_blocking_the_rest(self):
        manager = ConnectionManager()
        room_id = uuid.uuid4()
        dead, alive = _FakeSocket(fails=True), _FakeSocket()
        manager.connect(room_id, dead)
        manager.connect(room_id, alive)

        await manager.broadcast_local(room_id, {"type": "message"})

        assert alive.sent == [{"type": "message"}]
        assert manager.local_sockets_for_room(room_id) == {alive}

    async def test_disconnect_removes_the_room_entirely_once_empty(self):
        manager = ConnectionManager()
        room_id = uuid.uuid4()
        socket = _FakeSocket()
        manager.connect(room_id, socket)
        manager.disconnect(room_id, socket)

        assert manager.local_sockets_for_room(room_id) == set()
        assert room_id not in manager._rooms  # noqa: SLF001 -- internal-state assertion, this test IS the spec for it


# ---------------------------------------------------------------------
# DB fixtures shared by chat_service persistence tests and the WS
# integration tests -- raw SQL, same established reason as every other
# test file in this codebase: SQLite's DDL compiler has no
# `visit_ARRAY`/native-enum support for `User.skills`, and
# `User.bookmarks`'s `lazy="selectin"` relationship needs `user_bookmarks`
# to exist even though no test here ever reads `.bookmarks` directly.
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

_CREATE_CHAT_ROOMS_TABLE_SQL = """
CREATE TABLE chat_rooms (
    id VARCHAR(36) PRIMARY KEY,
    name VARCHAR(255),
    is_group BOOLEAN NOT NULL DEFAULT 0,
    is_auto_assigned BOOLEAN NOT NULL DEFAULT 0,
    max_members INTEGER,
    course_id VARCHAR(36),
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_CHAT_PARTICIPANTS_TABLE_SQL = """
CREATE TABLE chat_participants (
    room_id VARCHAR(36) NOT NULL,
    user_id VARCHAR(36) NOT NULL,
    joined_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_pinned BOOLEAN NOT NULL DEFAULT 0,
    last_active_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_muted BOOLEAN NOT NULL DEFAULT 0,
    PRIMARY KEY (room_id, user_id)
)
"""

_CREATE_CHAT_MESSAGES_TABLE_SQL = """
CREATE TABLE chat_messages (
    id VARCHAR(36) PRIMARY KEY,
    room_id VARCHAR(36) NOT NULL,
    sender_id VARCHAR(36) NOT NULL,
    content TEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_bot BOOLEAN NOT NULL DEFAULT 0
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
        await conn.execute(text(_CREATE_CHAT_ROOMS_TABLE_SQL))
        await conn.execute(text(_CREATE_CHAT_PARTICIPANTS_TABLE_SQL))
        await conn.execute(text(_CREATE_CHAT_MESSAGES_TABLE_SQL))

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


async def _make_user(db: AsyncSession, *, full_name: str = "Test Student") -> User:
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.com", full_name=full_name)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


# ---------------------------------------------------------------------
# Component 4: chat_service persistence.
# ---------------------------------------------------------------------


class TestChatServicePersistence:
    pytestmark = pytest.mark.asyncio

    async def test_get_or_create_dm_room_creates_a_room_with_both_participants(
        self, db_session_factory
    ):
        async with db_session_factory() as db:
            alice = await _make_user(db, full_name="Alice")
            bob = await _make_user(db, full_name="Bob")

            room = await chat_service.get_or_create_dm_room(db, alice.id, bob.id)

            assert room.is_group is False
            assert room.name is None
            assert await chat_service.is_participant(db, room.id, alice.id) is True
            assert await chat_service.is_participant(db, room.id, bob.id) is True

    async def test_get_or_create_dm_room_reuses_the_existing_room(self, db_session_factory):
        async with db_session_factory() as db:
            alice = await _make_user(db, full_name="Alice")
            bob = await _make_user(db, full_name="Bob")

            first = await chat_service.get_or_create_dm_room(db, alice.id, bob.id)
            # Reversed argument order -- must still find the SAME room.
            second = await chat_service.get_or_create_dm_room(db, bob.id, alice.id)

            assert first.id == second.id

            count_result = await db.execute(
                text("SELECT COUNT(*) FROM chat_rooms WHERE is_group = 0")
            )
            assert count_result.scalar_one() == 1

    async def test_get_or_create_dm_room_rejects_self_dm(self, db_session_factory):
        async with db_session_factory() as db:
            alice = await _make_user(db, full_name="Alice")
            with pytest.raises(chat_service.SelfDMError):
                await chat_service.get_or_create_dm_room(db, alice.id, alice.id)

    async def test_create_group_room_adds_creator_and_members(self, db_session_factory):
        async with db_session_factory() as db:
            creator = await _make_user(db, full_name="Creator")
            friend_a = await _make_user(db, full_name="Friend A")
            friend_b = await _make_user(db, full_name="Friend B")

            room = await chat_service.create_group_room(
                db, creator.id, "Study Squad", [friend_a.id, friend_b.id]
            )

            assert room.is_group is True
            assert room.name == "Study Squad"
            participants = await chat_service.get_room_participants(db, room.id)
            participant_ids = {p.id for p in participants}
            assert participant_ids == {creator.id, friend_a.id, friend_b.id}

    async def test_create_group_room_rejects_a_group_of_just_the_creator(self, db_session_factory):
        async with db_session_factory() as db:
            creator = await _make_user(db, full_name="Creator")
            with pytest.raises(chat_service.EmptyGroupError):
                await chat_service.create_group_room(db, creator.id, "Solo Group", [creator.id])

    async def test_list_my_rooms_only_returns_rooms_the_user_belongs_to(self, db_session_factory):
        async with db_session_factory() as db:
            alice = await _make_user(db, full_name="Alice")
            bob = await _make_user(db, full_name="Bob")
            carol = await _make_user(db, full_name="Carol")

            shared_room = await chat_service.get_or_create_dm_room(db, alice.id, bob.id)
            await chat_service.get_or_create_dm_room(db, bob.id, carol.id)

            alice_rooms = await chat_service.list_my_rooms(db, alice.id)

            assert [r.id for r in alice_rooms] == [shared_room.id]

    async def test_save_message_persists_the_real_content_and_sender(self, db_session_factory):
        """Exercises `save_message` itself (the actual INSERT/commit
        path) -- ordering across MULTIPLE rapid saves is covered
        separately below with explicit timestamps, since SQLite's
        `CURRENT_TIMESTAMP` only has whole-second resolution (unlike
        Postgres's real microsecond-precision `now()`), so several
        `save_message` calls fired within the same test can land on the
        exact same `created_at` here in a way that would never happen
        against the real production database -- a SQLite test-harness
        artifact, not a real ordering bug in `get_message_history`'s own
        query (see the next two tests, which prove that query's ORDER BY
        is correct using rows with distinct, explicit timestamps)."""
        async with db_session_factory() as db:
            alice = await _make_user(db, full_name="Alice")
            bob = await _make_user(db, full_name="Bob")
            room = await chat_service.get_or_create_dm_room(db, alice.id, bob.id)

            message = await chat_service.save_message(db, room.id, alice.id, "Hey Bob!")

            assert message.content == "Hey Bob!"
            assert message.sender_id == alice.id
            assert message.room_id == room.id

            history = await chat_service.get_message_history(db, room.id)
            assert [m.content for m in history] == ["Hey Bob!"]

    async def test_message_history_orders_oldest_first(self, db_session_factory):
        async with db_session_factory() as db:
            alice = await _make_user(db, full_name="Alice")
            bob = await _make_user(db, full_name="Bob")
            room = await chat_service.get_or_create_dm_room(db, alice.id, bob.id)

            # Explicit, strictly increasing timestamps -- see
            # `test_save_message_persists_the_real_content_and_sender`'s
            # docstring for why this test builds rows directly rather
            # than via several rapid `save_message` calls.
            base = datetime(2026, 1, 1, tzinfo=timezone.utc)
            for i, (sender, text_) in enumerate(
                [(alice, "Hey Bob!"), (bob, "Hey Alice!"), (alice, "Ready for the exam?")]
            ):
                db.add(
                    ChatMessage(
                        room_id=room.id, sender_id=sender.id, content=text_,
                        created_at=base.replace(second=i),
                    )
                )
            await db.commit()

            history = await chat_service.get_message_history(db, room.id)

            assert [m.content for m in history] == ["Hey Bob!", "Hey Alice!", "Ready for the exam?"]
            assert [m.sender_id for m in history] == [alice.id, bob.id, alice.id]

    async def test_message_history_respects_the_limit(self, db_session_factory):
        async with db_session_factory() as db:
            alice = await _make_user(db, full_name="Alice")
            bob = await _make_user(db, full_name="Bob")
            room = await chat_service.get_or_create_dm_room(db, alice.id, bob.id)

            base = datetime(2026, 1, 1, tzinfo=timezone.utc)
            for i in range(5):
                db.add(
                    ChatMessage(
                        room_id=room.id, sender_id=alice.id, content=f"message {i}",
                        created_at=base.replace(second=i),
                    )
                )
            await db.commit()

            history = await chat_service.get_message_history(db, room.id, limit=2)

            # Most recent 2, oldest-first within that page.
            assert [m.content for m in history] == ["message 3", "message 4"]

    async def test_is_participant_is_false_for_a_non_member(self, db_session_factory):
        async with db_session_factory() as db:
            alice = await _make_user(db, full_name="Alice")
            bob = await _make_user(db, full_name="Bob")
            carol = await _make_user(db, full_name="Carol")
            room = await chat_service.get_or_create_dm_room(db, alice.id, bob.id)

            assert await chat_service.is_participant(db, room.id, carol.id) is False

    async def test_get_room_or_raise_on_missing_room(self, db_session_factory):
        async with db_session_factory() as db:
            with pytest.raises(chat_service.RoomNotFoundError):
                await chat_service.get_room_or_raise(db, uuid.uuid4())


# ---------------------------------------------------------------------
# Component 5: the WebSocket endpoint's full flow -- real HTTP/WS layer
# via TestClient, `get_db` overridden (same technique as
# `tests/test_admin_moderation.py`'s `client_factory`), real JWTs minted
# against this process's own `settings.supabase_jwt_secret`, and
# `publish_message` patched to call `manager.broadcast_local` directly
# (standing in for "a pub/sub listener task is running somewhere and
# will re-deliver this," which is true in the real app via `app/main.py`'s
# lifespan but not spun up under `TestClient` here -- see that module's
# own docstring for why this substitution is a faithful one: it's
# EXACTLY what the real listener itself does with a message it receives).
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
    """This sandbox's own `.env` has `SUPABASE_JWT_SECRET=` (empty) --
    fine for every OTHER test file (none of them exercise a real
    `decode_supabase_access_token` call; they all override
    `get_current_user` directly instead). This is the first test file
    that mints and verifies a REAL signed JWT end-to-end (see this
    module's own docstring for why `get_websocket_user` can't be
    bypassed via `app.dependency_overrides` the way `get_current_user`
    is elsewhere), so it needs a real, non-empty secret -- `jwt.encode`
    itself refuses an empty HMAC key outright. Scoped to this module only
    (autouse on THIS file's own tests, not global), and reverted
    automatically by `monkeypatch` after each test.
    """
    monkeypatch.setattr(settings, "supabase_jwt_secret", "test-only-signing-secret-do-not-use-in-prod")


@pytest.fixture
def chat_client(db_session_factory, fake_redis, monkeypatch):
    import app.services.chat_connection_manager as ccm

    async def _broadcast_directly(room_id, payload):
        await ccm.manager.broadcast_local(room_id, payload)

    monkeypatch.setattr(ccm, "publish_message", _broadcast_directly)
    # The router module imported `publish_message` by name (`from
    # app.services.chat_connection_manager import manager, publish_message`)
    # -- patching the SOURCE module's attribute above does not change
    # what that already-bound name inside the router module points to,
    # so it must ALSO be patched there directly.
    import app.api.v1.endpoints.chat as chat_router_module

    monkeypatch.setattr(chat_router_module, "publish_message", _broadcast_directly)

    async def _override_get_db():
        async with db_session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


class TestWebSocketFlow:
    pytestmark = pytest.mark.asyncio

    async def test_rejects_a_connection_with_no_token(self, chat_client):
        with chat_client.websocket_connect(f"/api/v1/chat/ws/{uuid.uuid4()}") as ws:
            event = ws.receive()
            assert event["type"] == "websocket.close"
            assert event["code"] == 4401

    async def test_rejects_an_authenticated_user_who_is_not_a_participant(
        self, db_session_factory, chat_client
    ):
        async with db_session_factory() as db:
            alice = await _make_user(db, full_name="Alice")
            bob = await _make_user(db, full_name="Bob")
            room = await chat_service.get_or_create_dm_room(db, alice.id, bob.id)
            carol = await _make_user(db, full_name="Carol")

        token = _mint_token(carol.id)
        with chat_client.websocket_connect(f"/api/v1/chat/ws/{room.id}?token={token}") as ws:
            first = ws.receive()
            assert first["type"] == "websocket.send"
            second = ws.receive()
            assert second["type"] == "websocket.close"
            assert second["code"] == 4403

    async def test_rejects_a_nonexistent_room(self, db_session_factory, chat_client):
        async with db_session_factory() as db:
            alice = await _make_user(db, full_name="Alice")

        token = _mint_token(alice.id)
        with chat_client.websocket_connect(f"/api/v1/chat/ws/{uuid.uuid4()}?token={token}") as ws:
            ws.receive()  # the ErrorEvent JSON frame
            close = ws.receive()
            assert close["code"] == 4404

    async def test_english_message_is_saved_and_broadcast_to_the_other_participant(
        self, db_session_factory, chat_client
    ):
        async with db_session_factory() as db:
            alice = await _make_user(db, full_name="Alice")
            bob = await _make_user(db, full_name="Bob")
            room = await chat_service.get_or_create_dm_room(db, alice.id, bob.id)

        alice_token = _mint_token(alice.id)
        bob_token = _mint_token(bob.id)

        with chat_client.websocket_connect(
            f"/api/v1/chat/ws/{room.id}?token={alice_token}"
        ) as alice_ws, chat_client.websocket_connect(
            f"/api/v1/chat/ws/{room.id}?token={bob_token}"
        ) as bob_ws:
            alice_ws.send_text('{"content": "Hey Bob, ready for the exam?"}')

            alice_received = alice_ws.receive_json()
            bob_received = bob_ws.receive_json()

        assert alice_received["type"] == "message"
        assert alice_received["content"] == "Hey Bob, ready for the exam?"
        assert alice_received["sender_id"] == str(alice.id)
        # Broadcast reaches EVERY participant, including the sender.
        assert bob_received == alice_received

        async with db_session_factory() as db:
            history = await chat_service.get_message_history(db, room.id)
            assert len(history) == 1
            assert history[0].content == "Hey Bob, ready for the exam?"

    async def test_arabic_message_bans_the_sender_and_closes_the_connection(
        self, db_session_factory, chat_client, fake_redis
    ):
        async with db_session_factory() as db:
            alice = await _make_user(db, full_name="Alice")
            bob = await _make_user(db, full_name="Bob")
            room = await chat_service.get_or_create_dm_room(db, alice.id, bob.id)

        token = _mint_token(alice.id)
        with chat_client.websocket_connect(f"/api/v1/chat/ws/{room.id}?token={token}") as ws:
            ws.send_text('{"content": "مرحبا يا صديقي"}')

            banned_event = ws.receive_json()
            assert banned_event == {
                "type": "banned",
                "reason": "Arabic detected. You are banned from chat for 30 minutes.",
                "banned_until_seconds": 1800,
            }

            close = ws.receive()
            assert close["type"] == "websocket.close"
            assert close["code"] == 4429

        assert await chat_security.is_user_banned(alice.id) is True

        async with db_session_factory() as db:
            history = await chat_service.get_message_history(db, room.id)
            assert history == [], "the Arabic message must never be saved to the database"

    async def test_a_currently_banned_user_is_rejected_on_connect(
        self, db_session_factory, chat_client, fake_redis
    ):
        async with db_session_factory() as db:
            alice = await _make_user(db, full_name="Alice")
            bob = await _make_user(db, full_name="Bob")
            room = await chat_service.get_or_create_dm_room(db, alice.id, bob.id)

        await chat_security.ban_user(alice.id, duration_seconds=900)
        token = _mint_token(alice.id)

        with chat_client.websocket_connect(f"/api/v1/chat/ws/{room.id}?token={token}") as ws:
            banned_event = ws.receive_json()
            assert banned_event["type"] == "banned"
            assert banned_event["banned_until_seconds"] == 900

            close = ws.receive()
            assert close["code"] == 4429
