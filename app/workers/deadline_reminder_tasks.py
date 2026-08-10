"""Smart Notifications Engine -- Approaching Deadlines periodic scan.

DISCLOSED, NECESSARY ADDITION: this step's own ask names "Approaching
Deadlines" as one of its three example trigger events, alongside Video
Approvals and New Badges Earned. Those other two already have a natural,
existing call site each (a state transition that happens exactly once --
a video becoming APPROVED, `add_xp` unlocking a badge). "Approaching
Deadlines" has no equivalent event to hook into: a `DeadlineAlert`
becoming "approaching" isn't a state transition anything in this
codebase currently DOES -- it's simply a fact that becomes true as real
wall-clock time passes, whether or not any request happens to touch that
row. Some form of periodic, time-driven check is therefore unavoidable,
not an invented feature -- this codebase already has exactly this shape
of periodic Celery Beat task precedent (`app/workers/aggregator_tasks.
run_daily_opportunity_scraper`, the Hybrid Opportunity Aggregation
Engine's own daily scraper), reused here rather than a new mechanism.

DISCLOSED JUDGMENT CALLS -- neither named by this step's own ask:

1. "Approaching" = within `APPROACHING_DEADLINE_WINDOW_DAYS` (3) days,
   and not yet past. Not a validated number -- same "reasonable,
   illustrative default" posture as `gamification_service`'s own
   `DAILY_LOGIN_XP`/`COURSE_COMPLETION_XP`/`UGC_APPROVAL_XP` constants.
2. Runs once daily at 05:00 UTC (`celery_app.py`'s `beat_schedule`) --
   a different hour than the aggregator's own 03:00 UTC slot, so the
   two daily jobs don't contend for the same minute.
3. `DeadlineAlert.reminder_sent` (a new column -- see that model's own
   comment) prevents the same alert from generating a fresh WARNING
   notification every single day it remains inside the approaching
   window; without it, a 3-day-out deadline would fire three near-
   identical notifications before it actually arrives.
"""

import asyncio
import datetime as dt
import logging

from sqlalchemy import select

from app.core.celery_app import celery_app
from app.db.session import AsyncSessionLocal, engine
from app.models.deadline_alert import DeadlineAlert
from app.services import notification_service

logger = logging.getLogger(__name__)

APPROACHING_DEADLINE_WINDOW_DAYS = 3


@celery_app.task(name="app.workers.deadline_reminder_tasks.check_approaching_deadlines_task")
def check_approaching_deadlines_task() -> dict[str, int]:
    """Celery entrypoint (sync). Same `asyncio.run()`/`engine.dispose()`
    event-loop-lifetime pattern as every other periodic task in this
    codebase -- see `app/workers/aggregator_tasks.py`'s own docstring for
    the full reasoning."""
    return asyncio.run(_check_approaching_deadlines())


async def _check_approaching_deadlines() -> dict[str, int]:
    checked = 0
    notified = 0
    errors = 0

    try:
        async with AsyncSessionLocal() as session:
            now = dt.datetime.now(dt.timezone.utc)
            window_end = now + dt.timedelta(days=APPROACHING_DEADLINE_WINDOW_DAYS)

            result = await session.execute(
                select(DeadlineAlert).where(
                    DeadlineAlert.is_completed.is_(False),
                    DeadlineAlert.reminder_sent.is_(False),
                    DeadlineAlert.scheduled_date >= now,
                    DeadlineAlert.scheduled_date <= window_end,
                )
            )
            alerts = list(result.scalars().all())

            for alert in alerts:
                checked += 1
                # Best-effort, per-alert -- one bad notification (a Redis
                # or DB hiccup) must not abort the rest of this run and
                # skip every OTHER genuinely-approaching deadline, same
                # posture as `aggregator_tasks`'s own per-post try/except.
                try:
                    days_remaining = (alert.scheduled_date - now).days
                    await notification_service.create_notification(
                        session,
                        alert.user_id,
                        "WARNING",
                        "Deadline approaching!",
                        f'"{alert.title}" is due in {days_remaining} day(s) '
                        f"({alert.scheduled_date.date().isoformat()}).",
                    )
                    alert.reminder_sent = True
                    session.add(alert)
                    await session.commit()
                    notified += 1
                except Exception:
                    logger.exception(
                        "Notifications: failed to send approaching-deadline reminder for alert %s",
                        alert.id,
                    )
                    await session.rollback()
                    errors += 1
    finally:
        # Same reasoning as every other Celery task's own `engine.
        # dispose()` call in this codebase -- see
        # `app/workers/scholar_agent.py`'s own docstring.
        await engine.dispose()

    result_summary = {"checked": checked, "notified": notified, "errors": errors}
    logger.info("Approaching-deadline reminder scan complete: %s", result_summary)
    return result_summary
