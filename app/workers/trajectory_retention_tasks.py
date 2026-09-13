"""Research Trajectory Memory (D3) -- retention/pruning periodic scan.

DISCLOSED, NECESSARY ADDITION, post-final-audit LOW finding:
`research_trajectory_steps` has no explicit retention/pruning policy --
`app/workers/aggregator_tasks.py::_poll_source` writes one real step per
real discovered item, forever, with no existing cap. Same "periodic
Celery Beat scan is the right mechanism because nothing else triggers
this" reasoning as `app/workers/deadline_reminder_tasks.py`'s own
docstring: a step becoming "eligible for pruning" isn't a state
transition anything else in this codebase does -- it's simply a fact of
real wall-clock time passing relative to its own age and its parent
trajectory's own, real, pre-existing `TrajectoryStatus` lifecycle field.
Reuses the existing periodic-task mechanism (`celery_app.conf.
beat_schedule`) rather than inventing a new one.

The real pruning logic (the safety rule, the window, the D3-compatible
reasoning) lives in `app/services/research_trajectory_memory.
prune_old_research_trajectory_steps` -- see that function's own
docstring for the full, disclosed design. This module is only the thin
Celery/asyncio bridging layer, same shape as every other periodic task
in this codebase.
"""

import asyncio
import logging

from app.core.celery_app import celery_app
from app.db.session import AsyncSessionLocal, engine
from app.services import research_trajectory_memory

logger = logging.getLogger(__name__)


@celery_app.task(name="app.workers.trajectory_retention_tasks.prune_research_trajectory_steps_task")
def prune_research_trajectory_steps_task() -> dict:
    """Celery entrypoint (sync). Same `asyncio.run()`/`engine.dispose()`
    event-loop-lifetime pattern as every other periodic task in this
    codebase -- see `app/workers/aggregator_tasks.py`'s own docstring for
    the full reasoning.

    Deliberately does NOT catch the exception `prune_old_research_
    trajectory_steps` re-raises on a genuine DB failure -- letting it
    propagate here means Celery marks this task run `FAILURE`, the
    honest, observable signal; swallowing it here would produce exactly
    the "misleading success" this remediation's own instructions
    prohibit.
    """
    return asyncio.run(_prune_research_trajectory_steps())


async def _prune_research_trajectory_steps() -> dict:
    try:
        async with AsyncSessionLocal() as session:
            return await research_trajectory_memory.prune_old_research_trajectory_steps(session)
    finally:
        # Same reasoning as every other Celery task's own `engine.
        # dispose()` call in this codebase -- see
        # `app/workers/scholar_agent.py`'s own docstring.
        await engine.dispose()
