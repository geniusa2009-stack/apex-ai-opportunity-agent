import asyncio
import logging
from datetime import datetime

from sqlalchemy import select

from app.core.celery_app import celery_app
from app.core.config import settings
from app.db.session import AsyncSessionLocal, engine
from app.models import Opportunity
from app.models.enums import AcademicLevel, Gender
from app.services.ai import ai_gateway

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Backfill Real Eligibility Data step additions -- defensive coercion
# from Gemini's free-text `target_gender`/`target_academic_levels`
# strings onto this codebase's real enum vocabulary. Same established
# posture as `aggregator_service._coerce_opportunity_type`: a value that
# doesn't cleanly match is logged and dropped, not allowed to crash the
# whole verification run over one bad sub-field.
# ---------------------------------------------------------------------


def _coerce_gender(raw_value: str | None, opportunity_id) -> Gender | None:
    if not raw_value:
        return None
    try:
        return Gender(raw_value.strip().lower())
    except ValueError:
        logger.warning(
            "Opportunity %s: AI returned unrecognized target_gender %r -- leaving null",
            opportunity_id,
            raw_value,
        )
        return None


def _coerce_academic_levels(raw_values: list[str] | None, opportunity_id) -> list[AcademicLevel] | None:
    if not raw_values:
        return None
    levels: list[AcademicLevel] = []
    for raw in raw_values:
        try:
            levels.append(AcademicLevel(raw.strip().lower()))
        except ValueError:
            logger.warning(
                "Opportunity %s: AI returned unrecognized academic level %r -- dropping it",
                opportunity_id,
                raw,
            )
    # An empty list after dropping every unrecognized entry is treated the
    # same as "the model returned nothing usable" -- `None` (unrestricted),
    # not an empty-list value with different (and untested) filter semantics.
    return levels or None


@celery_app.task(name="app.workers.tasks.verify_new_opportunities")
def verify_new_opportunities(limit: int = 20) -> dict:
    """Celery entrypoint (sync).

    Backfill Real Eligibility Data step -- DESIGN DECISION, disclosed:
    the task asking for this named `app/services/aggregator_service.py`
    (`AIExtractedOpportunityFields`) as the place to add age/gender/
    academic-level extraction. Reading that file first (as instructed)
    found a better real target: `aggregator_service.py`'s own extraction
    only runs against its MOCKED Telegram/Facebook ingestion (no real
    API credentials wired -- see that module's own docstring), and every
    row it creates enters this exact `is_verified=False,
    ai_confidence_score=None` queue below regardless, where THIS
    function's own verification call (`VerifiedOpportunitySchema`, app/
    services/ai.py) already overwrites title/deadline/eligibility_criteria/
    funding_details unconditionally. Duplicating age/gender/academic-level
    extraction into the first pass would be redundant work this second
    pass immediately supersedes -- same "don't build a second parallel
    mechanism" reasoning aggregator_service.py's own module docstring
    already uses for embeddings/legitimacy-checking.
    More importantly: THIS function is the one real, single funnel every
    `Opportunity` row passes through regardless of source (the pre-existing
    Scrapy pipeline's rows AND the aggregator's rows both land in the same
    `is_verified=False` queue this function polls) -- extracting eligibility
    fields here, not in aggregator_service.py, means real Scrapy-scraped
    rows get real eligibility data too, not just (currently-mocked, non-
    production) Telegram/Facebook rows. It's also the only place with real
    retained source text to re-run for backfilling EXISTING rows (see
    `scripts/backfill_opportunity_eligibility.py`): the Scrapy pipeline
    persists the full raw scrape verbatim into `raw_extracted_data`
    (confirmed via app/scraper/items.py's own docstring), which this same
    `verify_opportunity` call already consumes.
    `AIExtractedOpportunityFields`/`OpportunityCreate` were deliberately
    NOT extended with these four fields -- confirmed unnecessary by the
    above, not skipped.

    Celery's default (prefork) worker calls
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
                    # Backfill Real Eligibility Data step additions -- see
                    # `VerifiedOpportunitySchema`'s own field docs
                    # (app/services/ai.py) for the "null is the correct
                    # default" extraction rules these four values already
                    # went through inside the Gemini call itself.
                    opportunity.min_age = verification.min_age
                    opportunity.max_age = verification.max_age
                    opportunity.target_gender = _coerce_gender(verification.target_gender, opportunity.id)
                    opportunity.target_academic_levels = _coerce_academic_levels(
                        verification.target_academic_levels, opportunity.id
                    )
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
