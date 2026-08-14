"""Weekly League System -- weekly promote/demote/reset cron.

Same periodic Celery Beat shape as `app/workers/chat_group_tasks.py`.

Real Leaderboard System, Level 2 addition: `reset_monthly_leaderboard_task`
below, modeled directly on `reset_weekly_leagues_task` -- same task/session
lifecycle shape, registered in the SAME `celery_app.conf.beat_schedule`
dict (see `app/core/celery_app.py`), just on a "1st of the month" cron
instead of "every Monday." Kept in this same file rather than a new one:
it's the same category of periodic gamification-period reset, and this
file's own name ("league_tasks") already covers "periodic leaderboard
period resets" broadly enough -- a monthly XP reset has no league/
promotion step of its own to justify a separate module (see
`gamification_service.reset_monthly_leaderboard`'s own docstring)."""

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


@celery_app.task(name="app.workers.league_tasks.reset_monthly_leaderboard_task")
def reset_monthly_leaderboard_task() -> dict[str, int]:
    return asyncio.run(_reset_monthly())


async def _reset_monthly() -> dict[str, int]:
    try:
        async with AsyncSessionLocal() as session:
            summary = await gamification_service.reset_monthly_leaderboard(session)
    finally:
        await engine.dispose()
    logger.info("Real Leaderboard System: monthly reset complete: %s", summary)
    return summary
