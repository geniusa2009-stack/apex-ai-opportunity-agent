"""Live 1:1 Video Sessions -- scheduled-call reminder periodic scan.

DISCLOSED, NECESSARY ADDITION, same shape as `app/workers/deadline_
reminder_tasks.py`'s own docstring: a `LiveSession` becoming "about to
start" isn't a state transition anything in this codebase currently
DOES -- it's simply a fact that becomes true as real wall-clock time
passes toward `scheduled_at`, whether or not any request happens to
touch that row. This step's own trigger was an explicit, disclosed gap
this project's own README already named after the Live 1:1 Video
Sessions step shipped: "no scheduled-time reminder notification for a
confirmed booking." A periodic, time-driven scan closes it, reusing the
exact `check_approaching_deadlines_task` shape (same `AsyncSessionLocal`/
`engine.dispose()` lifecycle, same per-row best-effort try/except, same
`reminder_sent` boolean guard against re-notifying) rather than inventing
a new periodic-task pattern for what is structurally the same problem.

DISCLOSED JUDGMENT CALLS -- none named by the original ask (this whole
step was self-directed, in response to "what do you think is still
missing" -- see this project's own conversation history, not a literal
spec):

1. Reminder window: `settings.live_session_reminder_minutes_before`
   (default 60) minutes before `scheduled_at`, not a fixed day-based
   window like `DeadlineAlert`'s 3 days -- a scholarship deadline is
   meaningfully "approaching" days out; a video CALL is only usefully
   "about to start" on the order of minutes, not days, before it
   actually begins. A real, disclosed config setting (not a hardcoded
   module constant like `APPROACHING_DEADLINE_WINDOW_DAYS`) since "how
   far ahead to warn someone about a call" is plausibly something a real
   deployment would want to tune without a code change.
2. Scan cadence: every 5 minutes (`celery_app.py`'s `beat_schedule`),
   NOT once daily like the deadline scan. A once-a-day scan checked
   against a 60-minute window would miss almost every session entirely
   (the window would need to happen to overlap the scan's single daily
   run) -- the scan frequency has to be meaningfully finer than the
   reminder window itself for this to actually catch sessions in time.
   Five minutes is a disclosed, reasonable default balancing "reminder
   arrives with useful lead time" against "don't hammer the database
   with a query every few seconds."
3. BOTH participants are notified, not just one -- unlike
   `DeadlineAlert` (single-owner: `user_id`), a `LiveSession` has two
   real parties (`requested_by_id`/`requested_with_id`) who both need to
   know their call is starting soon. One `LiveSession` row inside the
   window therefore produces two `Notification` rows (one per party),
   not one.
4. Notification `type="WARNING"` -- reused directly from `notification_
   service.HIGHLY_CRITICAL_TYPES` (see that module's own comment): a
   call starting soon is exactly the kind of time-sensitive event that
   set was designed to also trigger a redundant email for, on the same
   reasoning as an approaching scholarship deadline. No new
   notification type was added for this.
"""

import asyncio
import datetime as dt
import logging

from sqlalchemy import select

from app.core.celery_app import celery_app
from app.core.config import settings
from app.db.session import AsyncSessionLocal, engine
from app.models.enums import LiveSessionStatus
from app.models.live_session import LiveSession
from app.services import notification_service

logger = logging.getLogger(__name__)


@celery_app.task(name="app.workers.live_session_reminder_tasks.check_upcoming_live_sessions_task")
def check_upcoming_live_sessions_task() -> dict[str, int]:
    """Celery entrypoint (sync). Same `asyncio.run()`/`engine.dispose()`
    event-loop-lifetime pattern as every other periodic task in this
    codebase -- see `app/workers/deadline_reminder_tasks.py`'s own
    docstring for the full reasoning."""
    return asyncio.run(_check_upcoming_live_sessions())


async def _check_upcoming_live_sessions() -> dict[str, int]:
    checked = 0
    notified = 0
    errors = 0

    try:
        async with AsyncSessionLocal() as session:
            now = dt.datetime.now(dt.timezone.utc)
            window_end = now + dt.timedelta(minutes=settings.live_session_reminder_minutes_before)

            result = await session.execute(
                select(LiveSession).where(
                    LiveSession.status == LiveSessionStatus.CONFIRMED.value,
                    LiveSession.reminder_sent.is_(False),
                    LiveSession.scheduled_at >= now,
                    LiveSession.scheduled_at <= window_end,
                )
            )
            live_sessions = list(result.scalars().all())

            for live_session in live_sessions:
                checked += 1
                # Best-effort, per-session -- one bad notification (a
                # Redis or DB hiccup) must not abort the rest of this run
                # and skip every OTHER genuinely-upcoming call, same
                # posture as `deadline_reminder_tasks`'s own per-alert
                # try/except.
                try:
                    # DISCLOSED SQLite-ONLY DEFENSIVE FIX (mirrors
                    # `deadline_alerts.generate_milestones`'s own
                    # `deadline_utc = deadline if deadline.tzinfo is not
                    # None else deadline.replace(tzinfo=timezone.utc)`
                    # pattern): a real Postgres `DateTime(timezone=True)`
                    # column round-trips `tzinfo` correctly, but SQLite
                    # (used only by this project's own test suite, never
                    # in production here) silently drops it on read-back,
                    # which would otherwise raise `TypeError: can't
                    # subtract offset-naive and offset-aware datetimes`
                    # below -- caught by this file's own pytest suite,
                    # not a theoretical concern.
                    scheduled_at = live_session.scheduled_at
                    if scheduled_at.tzinfo is None:
                        scheduled_at = scheduled_at.replace(tzinfo=dt.timezone.utc)

                    minutes_remaining = max(
                        int((scheduled_at - now).total_seconds() // 60), 0
                    )
                    topic_suffix = f' ("{live_session.topic}")' if live_session.topic else ""
                    message = (
                        f"Your live session{topic_suffix} starts in "
                        f"{minutes_remaining} minute(s), at "
                        f"{scheduled_at.isoformat()}."
                    )
                    # Both parties get their own Notification row -- see
                    # this module's own docstring, judgment call 3.
                    for participant_id in (
                        live_session.requested_by_id,
                        live_session.requested_with_id,
                    ):
                        await notification_service.create_notification(
                            session,
                            participant_id,
                            "WARNING",
                            "Your live session starts soon!",
                            message,
                        )
                    live_session.reminder_sent = True
                    session.add(live_session)
                    await session.commit()
                    notified += 1
                except Exception:
                    logger.exception(
                        "Live sessions: failed to send upcoming-call reminder for session %s",
                        live_session.id,
                    )
                    await session.rollback()
                    errors += 1
    finally:
        # Same reasoning as every other Celery task's own `engine.
        # dispose()` call in this codebase -- see
        # `app/workers/scholar_agent.py`'s own docstring.
        await engine.dispose()

    result_summary = {"checked": checked, "notified": notified, "errors": errors}
    logger.info("Upcoming-live-session reminder scan complete: %s", result_summary)
    return result_summary
