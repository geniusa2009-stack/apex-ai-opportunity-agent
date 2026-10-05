"""Celery entry points for the APEX AI Opportunity Agent.

The Celery task is only a *trigger*: all job state lives in the
`pipeline_jobs` table, so a lost Celery message, a worker restart or a
duplicate trigger is harmless -- the next run simply claims whatever is
due (leases prevent double processing).

* `run_agent_pipeline_task` -- every minute (beat) and on demand after
  ingestion/approval; claims and advances due jobs.
* `poll_agent_feeds_task`   -- every 30 minutes; polls the RSS/Atom feeds
  listed in AGENT_RSS_FEEDS (only feeds the team configured).

Same event-loop/engine-disposal pattern as `app/workers/tasks.py`.
"""

from __future__ import annotations

import asyncio
import logging

from app.core.celery_app import celery_app
from app.core.config import settings

logger = logging.getLogger(__name__)


async def _run_pipeline(limit: int | None = None) -> dict:
    from app.agent.pipeline import build_dependencies, run_due_jobs
    from app.db.session import AsyncSessionLocal, engine

    try:
        return await run_due_jobs(AsyncSessionLocal, build_dependencies(), limit=limit)
    finally:
        await engine.dispose()


async def _poll_feeds() -> dict:
    from app.agent.connectors.feeds import RSSConnector
    from app.agent.errors import AgentError
    from app.agent.ingestion import ingest_event
    from app.db.session import AsyncSessionLocal, engine

    summary: dict[str, int] = {}
    try:
        for feed_url in settings._csv(settings.agent_rss_feeds):
            try:
                events = await RSSConnector(feed_url).fetch()
            except AgentError as exc:
                logger.warning("feed %s failed: %s", feed_url, exc.code)
                summary["feed_errors"] = summary.get("feed_errors", 0) + 1
                continue
            async with AsyncSessionLocal() as db:
                for event in events:
                    # Feeds are configured by the team -> their Source is approved.
                    result = await ingest_event(db, event, auto_approve_source=True)
                    summary[result.status.value] = summary.get(result.status.value, 0) + 1
                await db.commit()
        return summary
    finally:
        await engine.dispose()


@celery_app.task(name="app.workers.agent_tasks.run_agent_pipeline_task")
def run_agent_pipeline_task(limit: int | None = None) -> dict:
    return asyncio.run(_run_pipeline(limit))


@celery_app.task(name="app.workers.agent_tasks.poll_agent_feeds_task")
def poll_agent_feeds_task() -> dict:
    return asyncio.run(_poll_feeds())


def trigger_pipeline() -> None:
    """Best-effort immediate kick after ingestion/approval. If the broker is
    down nothing is lost: the beat schedule picks the job up later."""
    if not settings.agent_celery_trigger_enabled:
        return
    try:
        run_agent_pipeline_task.apply_async(retry=False, ignore_result=True)
    except Exception as exc:  # noqa: BLE001 - broker unavailable is not an error for the caller
        logger.info("agent pipeline trigger not sent (%s); beat will pick the job up", type(exc).__name__)
