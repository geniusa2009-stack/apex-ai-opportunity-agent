import asyncio
import logging
from datetime import datetime

from sqlalchemy import select

from app.core.celery_app import celery_app
from app.core.config import settings
from app.db.session import AsyncSessionLocal, engine
from app.models import Opportunity
from app.services.ai import ai_gateway

logger = logging.getLogger(__name__)


@celery_app.task(name="app.workers.tasks.verify_new_opportunities")
def verify_new_opportunities(limit: int = 20) -> dict:
    """Celery entrypoint (sync). Celery's default (prefork) worker calls
    plain functions, not coroutines -- `asyncio.run()` gives this task its
    own fresh event loop to run the actual async DB + AI Gateway work in.

    Why the loop matters here: our shared async engine
    (app.db.session.engine) lazily binds its asyncpg connections to
    whichever event loop is running the first time they're used.
    `asyncio.run()` tears its loop down when the function returns, so any
    pooled connections opened during THIS run are attached to a now-dead
    loop by the time the NEXT task starts a new one -- reusing them raises
    errors like "attached to a different loop" or "Event loop is closed".
    `_verify_new_opportunities` disposes the engine's pool in a `finally`
    block for exactly this reason: it forces the next task invocation to
    open fresh connections against its own new loop. That costs a small
    reconnect per task run, which is a non-issue at the cadence this task
    runs on, and far simpler than trying to keep one event loop alive
    across a Celery worker's whole lifetime.
    """
    return asyncio.run(_verify_new_opportunities(limit))


async def _verify_new_opportunities(limit: int) -> dict:
    processed = 0
    verified = 0
    flagged = 0
    errors = 0

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Opportunity)
                .where(
                    Opportunity.is_verified.is_(False),
                    Opportunity.ai_confidence_score.is_(None),
                )
                .limit(limit)
            )
            opportunities = result.scalars().all()

            for opportunity in opportunities:
                processed += 1
                try:
                    verification, embedding = await ai_gateway.verify_and_embed(
                        opportunity.raw_extracted_data or {}
                    )
                except Exception:
                    logger.exception(
                        "AI verification call failed for opportunity %s", opportunity.id
                    )
                    errors += 1
                    continue

                if (
                    verification.is_legitimate
                    and verification.confidence_score > settings.ai_confidence_threshold
                ):
                    opportunity.title = verification.title or opportunity.title
                    opportunity.deadline = _parse_deadline(verification.deadline, opportunity.id)
                    opportunity.eligibility_criteria = verification.eligibility_criteria
                    opportunity.funding_details = verification.funding_details
                    opportunity.embedding = embedding
                    opportunity.ai_confidence_score = verification.confidence_score
                    opportunity.is_verified = True
                    verified += 1
                else:
                    # Flagging mechanism: leave is_verified=False but record the
                    # score. That alone moves the row out of the "pending"
                    # query above (`ai_confidence_score IS NULL`) and into a
                    # de facto human-review queue -- reachable later via
                    # `WHERE is_verified = false AND ai_confidence_score IS NOT NULL`.
                    # Deliberately not deleting: raw_extracted_data is the audit
                    # trail, and a prompt fix or model upgrade can re-evaluate
                    # these later without re-scraping anything.
                    opportunity.ai_confidence_score = verification.confidence_score
                    flagged += 1

            await session.commit()
    finally:
        await engine.dispose()

    summary = {"processed": processed, "verified": verified, "flagged": flagged, "errors": errors}
    logger.info("verify_new_opportunities: %s", summary)
    return summary


def _parse_deadline(raw: str | None, opportunity_id) -> datetime | None:
    if not raw:
        return None
    try:
        # `datetime.fromisoformat` only accepts a trailing "Z" (meaning UTC)
        # starting in Python 3.11; normalizing it here makes this parser
        # correct regardless of the running Python version, rather than
        # silently depending on the Docker image staying on 3.11+.
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        logger.warning("Could not parse AI-returned deadline %r for opportunity %s", raw, opportunity_id)
        return None
