import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

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

app.include_router(api_router, prefix=settings.api_v1_prefix)


@app.get("/")
async def root() -> dict:
    return {"message": f"{settings.project_name} API is running"}
