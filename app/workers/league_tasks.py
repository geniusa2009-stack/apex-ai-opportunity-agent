"""Weekly League System -- weekly promote/demote/reset cron.

Same periodic Celery Beat shape as `app/workers/chat_group_tasks.py`."""

import asyncio
import logging

from app.core.celery_app import celery_app
from app.db.session import AsyncSessionLocal, engine
from app.services import gamification_service

logger = logging.getLogger(__name__)


@celery_app.task(name="app.workers.league_tasks.reset_weekly_leagues_task")
def reset_weekly_leagues_task() -> dict[str, int]:
    return asyncio.run(_reset())


async def _reset() -> dict[str, int]:
    try:
        async with AsyncSessionLocal() as session:
            summary = await gamification_service.reset_weekly_leagues(session)
    finally:
        await engine.dispose()
    logger.info("Weekly League System: weekly reset complete: %s", summary)
    return summary
