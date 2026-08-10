"""Auto-Assigned English Practice Groups -- 48-hour inactivity sweep.

Same periodic Celery Beat shape as `app/workers/deadline_reminder_tasks.py`
(see that module's own docstring for the full "why a periodic task at
all, and why the `asyncio.run()` + `engine.dispose()` pattern" reasoning,
which applies identically here) -- reused rather than inventing a second
mechanism for the same class of "this becomes true just from wall-clock
time passing, nothing else triggers it" problem.
"""

import asyncio
import logging

from app.core.celery_app import celery_app
from app.db.session import AsyncSessionLocal, engine
from app.services import chat_group_service

logger = logging.getLogger(__name__)


@celery_app.task(name="app.workers.chat_group_tasks.sweep_inactive_practice_group_members_task")
def sweep_inactive_practice_group_members_task() -> dict[str, int]:
    """Celery entrypoint (sync)."""
    return asyncio.run(_sweep())


async def _sweep() -> dict[str, int]:
    try:
        async with AsyncSessionLocal() as session:
            summary = await chat_group_service.sweep_inactive_participants(session)
    finally:
        await engine.dispose()

    logger.info("English Practice Groups: inactivity sweep complete: %s", summary)
    return summary
