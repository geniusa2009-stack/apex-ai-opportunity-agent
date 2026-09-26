"""Opportunity Monitoring & Personal Impact Engine, Part P: the periodic
Celery Beat entrypoint that actually runs
`opportunity_reverification.reverify_opportunity` against a bounded,
prioritized batch of already-published opportunities.

Same `asyncio.run()`/`engine.dispose()` event-loop-lifetime pattern as
every other periodic task in this codebase (see
`app/workers/aggregator_tasks.py`'s own docstring), and the same
per-item best-effort try/except posture as `deadline_reminder_tasks.py`
(Part V: one opportunity's re-verification failing must never abort the
rest of the batch).

BATCH_SIZE (25) and the once-daily cadence (`celery_app.py`'s
`beat_schedule`, 04:00 UTC -- between the scraper's 03:00 and the
deadline scan's 05:00 so none of the three daily jobs contend for the
same minute) are disclosed, illustrative defaults, not validated
numbers -- same posture as `deadline_reminder_tasks.
APPROACHING_DEADLINE_WINDOW_DAYS`. Each opportunity re-verification
makes one real Gemini/OpenAI call (`ai_gateway.verify_and_embed`), so
this cadence and batch size are deliberately conservative -- see the
report's own Known Limitations for the disclosed trade-off (a genuinely
adaptive, sub-daily monitoring cadence is a later infrastructure step,
per Part P's own explicit escape hatch: "if the current infrastructure
cannot support adaptive scheduling safely, implement the monitoring
service and deterministic prioritization first and document scheduling
as a later infrastructure step").
"""

import asyncio
import logging

from app.core.celery_app import celery_app
from app.db.session import AsyncSessionLocal, engine
from app.models.opportunity import Opportunity
from app.services.opportunity_monitoring_scheduler import find_opportunities_to_monitor
from app.services.opportunity_reverification import reverify_opportunity

logger = logging.getLogger(__name__)

BATCH_SIZE = 25


@celery_app.task(name="app.workers.monitoring_tasks.run_opportunity_monitoring_scan_task")
def run_opportunity_monitoring_scan_task() -> dict[str, int]:
    """Celery entrypoint (sync)."""
    return asyncio.run(_run_opportunity_monitoring_scan())


async def _run_opportunity_monitoring_scan() -> dict[str, int]:
    checked = 0
    changes_recorded = 0
    notifications_created = 0
    source_failures = 0
    errors = 0

    try:
        async with AsyncSessionLocal() as session:
            opportunity_ids = await find_opportunities_to_monitor(session, limit=BATCH_SIZE)

            for opportunity_id in opportunity_ids:
                # Best-effort, per-opportunity (Part V) -- one failure
                # here must never abort the rest of the batch.
                try:
                    opportunity = await session.get(Opportunity, opportunity_id)
                    if opportunity is None:
                        continue

                    outcome = await reverify_opportunity(session, opportunity=opportunity)
                    checked += 1

                    if outcome.ai_call_failed:
                        source_failures += 1
                    elif outcome.cycle_result is not None:
                        changes_recorded += len(outcome.cycle_result.recorded)
                        notifications_created += outcome.cycle_result.notifications_created
                except Exception:
                    logger.exception(
                        "opportunity_monitoring_scan: reverification failed for opportunity %s", opportunity_id,
                    )
                    await session.rollback()
                    errors += 1
    finally:
        await engine.dispose()

    result_summary = {
        "checked": checked,
        "changes_recorded": changes_recorded,
        "notifications_created": notifications_created,
        "source_failures": source_failures,
        "errors": errors,
    }
    logger.info("Opportunity monitoring scan complete: %s", result_summary)
    return result_summary
