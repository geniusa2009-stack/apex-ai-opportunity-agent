"""Live 1:1 Video Sessions -- WebRTC signaling Connection Manager + Redis
Pub/Sub fan-out.

Architecturally IDENTICAL to `app/services/chat_connection_manager.py` --
same `ConnectionManager` (per-process, in-memory `dict[room_id,
set[WebSocket]]`) plus the same "one process's WebSocket handler receives
a message, publishes it to Redis, every process (including itself)
re-broadcasts to its own locally-connected sockets" cross-process fan-out
design, for the exact same reason: two participants' WebSocket
connections could land on two different Uvicorn/Gunicorn worker
processes, and a raw `WebSocket` object can never be handed across a
process boundary -- only Redis Pub/Sub (already-required infrastructure
in this codebase for Celery) can get an event from "the process that
received it" to "every other process that might be holding the other
participant's socket."

DISCLOSED JUDGMENT CALL -- a SEPARATE module/singleton/Redis channel,
not a reuse of `chat_connection_manager.manager`/`CHAT_BROADCAST_CHANNEL`,
even though the two are structurally identical. Chat rooms (`ChatRoom.id`)
and live sessions (`LiveSession.id`) are two independent UUID spaces from
two unrelated tables; reusing the SAME `manager` singleton would mean
`room_id`-keyed local socket buckets are shared across both features. A
UUIDv4 collision between a real `ChatRoom.id` and a real `LiveSession.id`
is astronomically unlikely, but "astronomically unlikely" is not a sound
basis for a routing/authorization boundary between two features that
should never be able to leak sockets into each other's rooms, even in
principle -- so this gets its own manager, own singleton, own Redis
channel (`live_session:broadcast`, distinct from `chat:broadcast`), at
the cost of a small amount of duplicated infrastructure code rather than
a shared blast radius.

`ROOM` here means "one `LiveSession.id`" -- unlike chat rooms (which can
have many participants), a live session room will realistically only
ever have at most 2 locally-connected sockets (the two named
participants), but the manager itself places no hard limit on that;
authorization (exactly WHICH 2 users are even allowed to connect) is
enforced one layer up, by `live_session_service.assert_can_join`, before
`connect()` is ever called.
"""

import asyncio
import json
import logging
import uuid
from typing import Any

import redis.asyncio as aioredis

from app.core.config import settings

logger = logging.getLogger(__name__)

LIVE_SESSION_BROADCAST_CHANNEL = "live_session:broadcast"


class ConnectionManager:
    """Per-process registry of locally-connected signaling WebSockets,
    keyed by `LiveSession.id`. See `chat_connection_manager.
    ConnectionManager`'s own docstring -- this is the same shape,
    deliberately duplicated rather than imported (see this module's own
    docstring for why)."""

    def __init__(self) -> None:
        self._rooms: dict[uuid.UUID, set[Any]] = {}

    def connect(self, session_id: uuid.UUID, websocket: Any) -> None:
        self._rooms.setdefault(session_id, set()).add(websocket)

    def disconnect(self, session_id: uuid.UUID, websocket: Any) -> None:
        sockets = self._rooms.get(session_id)
        if sockets is None:
            return
        sockets.discard(websocket)
        if not sockets:
            del self._rooms[session_id]

    async def broadcast_local(self, session_id: uuid.UUID, payload: dict, *, exclude: Any = None) -> None:
        """Relays `payload` to every socket THIS PROCESS currently has
        open for `session_id`, other than `exclude` (the socket that sent
        it in the first place, when called directly rather than via the
        pub/sub round-trip -- see `publish_signal`'s own docstring for
        why `exclude` is NOT used at the publish call site itself)."""
        sockets = self._rooms.get(session_id)
        if not sockets:
            return

        dead: list[Any] = []
        for socket in sockets:
            if socket is exclude:
                continue
            try:
                await socket.send_json(payload)
            except Exception:
                logger.warning("live_session: dropping a dead socket in session %s", session_id)
                dead.append(socket)

        for socket in dead:
            sockets.discard(socket)


manager = ConnectionManager()

_redis_client: aioredis.Redis | None = None


def _get_redis_client() -> aioredis.Redis:
    """Own lazy singleton, deliberately separate from both
    `chat_connection_manager._get_redis_client` and
    `notification_connection_manager`'s own -- same "a subscribed Pub/Sub
    connection can't be reused for ordinary commands" reasoning already
    disclosed in `chat_connection_manager.py`."""
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.Redis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


async def publish_signal(session_id: uuid.UUID, payload: dict) -> None:
    """Publishes one signaling message (an SDP offer/answer or an ICE
    candidate, opaque to this layer -- see the WebSocket handler in
    `app/api/v1/endpoints/live_sessions.py` for the envelope shape) onto
    the shared broadcast channel.

    The publishing process does NOT also call `manager.broadcast_local`
    directly -- exactly like `chat_connection_manager.publish_message`,
    it relies entirely on its own pub/sub subscription echoing the
    message back (see `run_live_session_pubsub_listener` below), so a
    single-process deployment and a multi-process one behave identically:
    every delivery goes through the SAME "publish, then receive via
    subscription" path, with nothing being delivered twice.
    """
    client = _get_redis_client()
    envelope = json.dumps({"session_id": str(session_id), "payload": payload})
    await client.publish(LIVE_SESSION_BROADCAST_CHANNEL, envelope)


async def _consume_pubsub_forever(client: aioredis.Redis) -> None:
    pubsub = client.pubsub()
    try:
        await pubsub.subscribe(LIVE_SESSION_BROADCAST_CHANNEL)
        logger.info("live_session: pub/sub listener subscribed to %s", LIVE_SESSION_BROADCAST_CHANNEL)

        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            try:
                envelope = json.loads(message["data"])
                session_id = uuid.UUID(envelope["session_id"])
                payload = envelope["payload"]
            except (KeyError, ValueError, TypeError):
                logger.warning("live_session: dropping a malformed pub/sub envelope: %r", message.get("data"))
                continue

            try:
                await manager.broadcast_local(session_id, payload)
            except Exception:
                logger.exception("live_session: broadcast_local failed for session %s", session_id)
    finally:
        try:
            await pubsub.unsubscribe(LIVE_SESSION_BROADCAST_CHANNEL)
            await pubsub.aclose()
        except Exception:
            logger.debug("live_session: pub/sub cleanup skipped (connection was never established)")


async def run_live_session_pubsub_listener() -> None:
    """Long-running background task, one per process -- see
    `chat_connection_manager.run_pubsub_listener`'s own docstring for the
    full disclosed reasoning behind the outer retry-with-backoff loop
    (identical here, duplicated rather than shared for the same
    "independent failure domains" posture `app/main.py`'s lifespan hook
    already establishes between the chat and notification listener
    tasks)."""
    backoff_seconds = 1.0
    while True:
        try:
            client = _get_redis_client()
            await _consume_pubsub_forever(client)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "live_session: pub/sub listener connection failed, retrying in %.0fs", backoff_seconds
            )
            try:
                await asyncio.sleep(backoff_seconds)
            except asyncio.CancelledError:
                raise
            backoff_seconds = min(backoff_seconds * 2, 30.0)
        else:
            backoff_seconds = 1.0
