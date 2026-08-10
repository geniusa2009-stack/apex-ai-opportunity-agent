"""AI-Powered Course Cohorts -- stale-question AI Bot auto-answer sweep.

Same periodic Celery Beat shape as `app/workers/chat_group_tasks.py`
(reused directly -- see that module's own docstring for the full
`asyncio.run()` + `engine.dispose()` reasoning)."""

import asyncio
import logging

from app.core.celery_app import celery_app
from app.db.session import AsyncSessionLocal, engine
from app.services import chat_group_service

logger = logging.getLogger(__name__)


@celery_app.task(name="app.workers.course_cohort_tasks.answer_stale_course_questions_task")
def answer_stale_course_questions_task() -> dict[str, int]:
    return asyncio.run(_answer())


async def _answer() -> dict[str, int]:
    try:
        async with AsyncSessionLocal() as session:
            summary = await chat_group_service.answer_stale_course_questions(session)
    finally:
        await engine.dispose()
    logger.info("Course Cohorts: AI Bot sweep complete: %s", summary)
    return summary
