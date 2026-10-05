import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

from app.agent.config_checks import run_startup_checks
from app.api.v1.router import api_router
from app.core.config import settings
from app.services.chat_connection_manager import run_pubsub_listener
from app.services.live_session_connection_manager import run_live_session_pubsub_listener
from app.services.notification_connection_manager import run_notification_pubsub_listener

logger = logging.getLogger(__name__)


# English-Only Gamified Real-time Chat -- this codebase's FIRST-ever
# application lifespan hook; no prior feature needed one (every earlier
# background job runs as a separate Celery worker process, not inside
# this FastAPI process's own lifecycle). This one is NOT optional: the
# WebSocket chat handler (`app/api/v1/endpoints/chat.py`) publishes every
# new message to Redis via `chat_connection_manager.publish_message`
# rather than delivering it locally -- see that module's own docstring
# for why. Without `run_pubsub_listener` actually running SOMEWHERE, NO
# process (including the very one that published the message) has
# anything subscribed to receive it back, so broadcast would silently
# deliver to nobody at all, even in a single-process deployment. This
# lifespan starts exactly one such listener task per process and cancels
# it cleanly on shutdown.
#
# Smart Notifications Engine -- a SECOND, independent listener task is
# started alongside the chat one, not instead of it: `notification_
# connection_manager.py` deliberately runs its own `ConnectionManager`
# instance and its own Redis Pub/Sub channel (see that module's own
# docstring for the full reasoning on why this is "extend," not
# "reuse-the-singleton"). Both tasks are started/cancelled the same way,
# independently -- one failing/restarting has no effect on the other.
#
# Live 1:1 Video Sessions -- a THIRD, equally independent listener task,
# for the WebRTC signaling relay's own Redis channel (`live_session_
# connection_manager.py`'s own docstring has the full "why a separate
# manager/channel, not a reuse of chat's" reasoning). Same start/cancel
# treatment as the other two.
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Fail fast on fatal misconfiguration (e.g. an embedding model whose
    # vector size does not match the pgvector column) -- see
    # app/agent/config_checks.py. Warnings are only logged.
    run_startup_checks()
    listener_task = asyncio.create_task(run_pubsub_listener())
    notification_listener_task = asyncio.create_task(run_notification_pubsub_listener())
    live_session_listener_task = asyncio.create_task(run_live_session_pubsub_listener())
    logger.info("chat: pub/sub listener task started")
    logger.info("notifications: pub/sub listener task started")
    logger.info("live_session: pub/sub listener task started")
    try:
        yield
    finally:
        listener_task.cancel()
        notification_listener_task.cancel()
        live_session_listener_task.cancel()
        for task in (listener_task, notification_listener_task, live_session_listener_task):
            try:
                await task
            except asyncio.CancelledError:
                pass
        logger.info("chat: pub/sub listener task stopped")
        logger.info("notifications: pub/sub listener task stopped")
        logger.info("live_session: pub/sub listener task stopped")


app = FastAPI(
    title=settings.project_name,
    version="0.1.0",
    description="Apex: a global aggregator for scholarships, internships, "
    "volunteer opportunities, and courses, with AI-powered verification "
    "and AI-assisted application document generation.",
    lifespan=lifespan,
)

# APEX Staging Deployment Readiness: additive-only CORS wiring -- see
# `Settings.cors_origins`'s own comment in app/core/config.py for why
# this was previously entirely absent and why an empty `CORS_ORIGINS`
# env var reproduces the exact prior behavior (no cross-origin access)
# rather than changing it. `allow_credentials=True` because the
# frontend sends the Supabase-issued bearer token via an `Authorization`
# header (not a cookie), which still requires this flag under CORS for
# the header itself to be allowed through on the preflight response --
# it does NOT mean cookies are shared cross-origin (none are used here).
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=settings.api_v1_prefix)


@app.get("/")
async def root() -> dict:
    return {"message": f"{settings.project_name} API is running"}


# APEX Staging Deployment Readiness: minimal, non-destructive health
# check. Deliberately separate from `GET /` (which is a human-facing
# "is this even the right API" sanity message, pre-existing, left
# unchanged) -- a deploy platform's own health-check prober needs a
# fast, dependency-free 200 it can poll every few seconds without that
# load ever touching the database or Redis. Returns no connection
# strings, secrets, tokens, or credentials -- only the fact that this
# process is alive.
@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# Readiness (APEX AI Opportunity Agent hardening). Unlike /health (pure
# liveness), this touches the real dependencies with short timeouts so a
# platform only routes traffic to an instance that can actually serve it.
# It never returns connection strings or secrets -- only check names.
@app.get("/ready")
async def ready() -> JSONResponse:
    from sqlalchemy import text

    from app.agent.config_checks import database_vector_dimension, model_vector_dimension
    from app.db.session import AsyncSessionLocal

    checks: dict[str, str] = {}

    try:
        async with AsyncSessionLocal() as session:
            await asyncio.wait_for(session.execute(text("SELECT 1")), timeout=3)
            checks["database"] = "ok"
            db_dim = await asyncio.wait_for(database_vector_dimension(session), timeout=3)
            if db_dim is not None and db_dim != settings.embedding_dimension:
                checks["embedding_dimension"] = (
                    f"mismatch: database vector({db_dim}) vs EMBEDDING_DIMENSION={settings.embedding_dimension}"
                )
            elif db_dim is not None and db_dim != model_vector_dimension():
                checks["embedding_dimension"] = "mismatch: database vs ORM model"
            else:
                checks["embedding_dimension"] = "ok"
    except Exception as exc:  # noqa: BLE001 - readiness must never raise
        checks["database"] = f"unavailable ({type(exc).__name__})"

    try:
        from redis import asyncio as aioredis

        client = aioredis.Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
        try:
            await asyncio.wait_for(client.ping(), timeout=3)
            checks["redis"] = "ok"
        finally:
            await client.aclose()
    except Exception as exc:  # noqa: BLE001
        checks["redis"] = f"unavailable ({type(exc).__name__})"

    is_ready = all(value == "ok" for value in checks.values())
    return JSONResponse(status_code=200 if is_ready else 503, content={"status": "ready" if is_ready else "not_ready", "checks": checks})
