from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

celery_app = Celery(
    "apex",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "app.workers.tasks",
        "app.workers.scholar_agent",
        "app.workers.memory_tasks",
        "app.workers.media_processing",
        # `app.tasks.student_video` (not `app.workers.*`) -- disclosed,
        # deliberate second Celery-task package; see app/tasks/__init__.py.
        "app.tasks.student_video",
        # Hybrid Opportunity Aggregation Engine -- see that module's own
        # docstring for the disclosed `app/worker/tasks/` (literal ask) ->
        # `app/workers/` (this codebase's established convention) path
        # deviation.
        "app.workers.aggregator_tasks",
        # Cloud Garbage Collection (IPFS Unpinning & R2 Deletion) -- same
        # disclosed `app/worker/tasks/` -> `app/workers/` path deviation
        # as `aggregator_tasks` above; see that module's own docstring.
        "app.workers.cleanup_tasks",
        # Smart Notifications Engine -- transactional email dispatch;
        # see that module's own docstring.
        "app.workers.notification_tasks",
        # Smart Notifications Engine -- Approaching Deadlines periodic
        # scan; see that module's own docstring.
        "app.workers.deadline_reminder_tasks",
        # Live 1:1 Video Sessions -- Upcoming-Call reminder periodic scan;
        # see that module's own docstring.
        "app.workers.live_session_reminder_tasks",
        # Auto-Assigned English Practice Groups -- 48-hour inactivity
        # sweep periodic task; see that module's own docstring.
        "app.workers.chat_group_tasks",
        # AI-Powered Course Cohorts -- stale-question AI Bot sweep; see
        # that module's own docstring.
        "app.workers.course_cohort_tasks",
        # Weekly League System -- weekly promote/demote/reset cron; see
        # that module's own docstring.
        "app.workers.league_tasks",
        # Research Trajectory Memory (D3) -- retention/pruning periodic
        # scan; see that module's own docstring.
        "app.workers.trajectory_retention_tasks",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)

# Hybrid Opportunity Aggregation Engine -- this codebase's FIRST periodic
# (Celery Beat) task; every other Celery task here is triggered on-demand
# (`.delay(...)` from a router/another task), so no `beat_schedule` existed
# before this step. This step's own ask names the task
# `run_daily_opportunity_scraper` and says "daily," but doesn't specify a
# time of day -- 03:00 UTC is a disclosed, deliberate choice: outside
# typical Egyptian daytime posting/browsing hours (UTC+2/+3), so this
# multi-post, multi-Gemini-call run doesn't compete for capacity with
# real-time user-facing traffic. Running Celery Beat itself (a separate
# process, `celery -A app.core.celery_app beat`) is a deployment step
# outside this codebase's own scope -- this `conf.beat_schedule` entry is
# inert unless a Beat process is actually started against this app.
celery_app.conf.beat_schedule = {
    "run-daily-opportunity-scraper": {
        "task": "app.workers.aggregator_tasks.run_daily_opportunity_scraper",
        "schedule": crontab(hour=3, minute=0),
    },
    # Smart Notifications Engine -- second daily periodic task, a
    # different hour (05:00 UTC) than the scraper above so the two don't
    # contend for the same minute. See `app/workers/deadline_reminder_
    # tasks.py`'s own docstring for the full "why a periodic task at
    # all" reasoning.
    "check-approaching-deadlines": {
        "task": "app.workers.deadline_reminder_tasks.check_approaching_deadlines_task",
        "schedule": crontab(hour=5, minute=0),
    },
    # Live 1:1 Video Sessions -- Upcoming-Call reminders. DISCLOSED
    # DEPARTURE from the two daily-cadence entries above: a 60-minute
    # reminder window (`settings.live_session_reminder_minutes_before`)
    # needs a scan frequency meaningfully finer than once a day, or it
    # would miss almost every session's window entirely. Every 5 minutes,
    # not a single fixed hour -- see `app/workers/live_session_reminder_
    # tasks.py`'s own docstring, judgment call 2, for the full reasoning.
    "check-upcoming-live-sessions": {
        "task": "app.workers.live_session_reminder_tasks.check_upcoming_live_sessions_task",
        "schedule": crontab(minute="*/5"),
    },
    # Auto-Assigned English Practice Groups -- 48-hour inactivity sweep.
    # Every 30 minutes: fine enough that no swept student waits much past
    # the 48-hour mark, coarse enough not to hammer the DB with a full
    # room/participant scan every few minutes like the live-session
    # reminder scan above (that one needs finer granularity because ITS
    # own reminder window is only 60 minutes wide; this sweep's own
    # window is 48 HOURS wide, so a 30-minute cadence is already <0.1% of
    # that window).
    "sweep-inactive-practice-group-members": {
        "task": "app.workers.chat_group_tasks.sweep_inactive_practice_group_members_task",
        "schedule": crontab(minute="*/30"),
    },
    # AI-Powered Course Cohorts -- stale-question AI Bot sweep. Every 30
    # minutes, same cadence reasoning as the practice-group sweep above:
    # its own window (2 hours) is wide enough that a 30-minute cadence is
    # already a small fraction of it.
    "answer-stale-course-questions": {
        "task": "app.workers.course_cohort_tasks.answer_stale_course_questions_task",
        "schedule": crontab(minute="*/30"),
    },
    # Weekly League System -- promote/demote/reset. Monday 00:00 UTC,
    # once a week, per this feature's own literal "reset leaderboards
    # weekly" spec.
    "reset-weekly-leagues": {
        "task": "app.workers.league_tasks.reset_weekly_leagues_task",
        "schedule": crontab(day_of_week=1, hour=0, minute=0),
    },
    # Real Leaderboard System, Level 2 -- monthly XP period reset. 1st of
    # the month, 00:00 UTC (`crontab(day_of_month=1, ...)` -- NOT a
    # `day_of_week` cron like the weekly entry above; a month has no
    # fixed weekday). No promote/demote step, unlike the weekly entry --
    # see `gamification_service.reset_monthly_leaderboard`'s own
    # docstring for why. `leaderboard.html`'s own monthly countdown must
    # stay in sync with this exact schedule -- see that page's own
    # comment on its countdown constant.
    "reset-monthly-leaderboard": {
        "task": "app.workers.league_tasks.reset_monthly_leaderboard_task",
        "schedule": crontab(day_of_month=1, hour=0, minute=0),
    },
    # Research Trajectory Memory (D3) -- retention/pruning. Daily at
    # 04:00 UTC: between the aggregator's own 03:00 UTC scraper run and
    # the deadline-reminder scan's 05:00 UTC slot, so all three daily
    # jobs run at distinct minutes. Daily cadence is deliberately coarse
    # -- this is low-churn maintenance against a 180-day retention
    # window (see `RESEARCH_TRAJECTORY_STEP_RETENTION_DAYS`'s own
    # docstring), not a time-sensitive scan like the two reminder tasks
    # above.
    "prune-research-trajectory-steps": {
        "task": "app.workers.trajectory_retention_tasks.prune_research_trajectory_steps_task",
        "schedule": crontab(hour=4, minute=0),
    },
}
