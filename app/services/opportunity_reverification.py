"""Opportunity Monitoring & Personal Impact Engine, Part A/D/P: the
FETCH+VERIFY source for monitoring the fields `aggregator_service.
upsert_opportunity`'s own candidate genuinely cannot cover --
`eligibility_criteria`/`funding_details`/`min_age`/`max_age`/
`target_gender`/`target_academic_levels`/`lifecycle_state`-adjacent
deadline re-reads -- on opportunities that are ALREADY published and
verified.

AUDITED FIRST (Part 0): the only existing code that ever WRITES these
fields is `app/workers/tasks.py::_verify_new_opportunities`, gated on
`Opportunity.is_verified.is_(False)` -- it never runs again once an
opportunity is verified. There is genuinely no re-verification pipeline
for already-published opportunities anywhere in this codebase before
this phase; this module is that missing piece, reusing the EXACT same
AI call (`ai_gateway.verify_and_embed`) `tasks.py` already uses, not a
second LLM integration.

SCOPE, DISCLOSED: this module does NOT re-run D6/Evidence or D7/
Contradiction-Hunter (see `opportunity_monitoring.py`'s own module
docstring for the full reasoning) -- it uses the SAME `is_legitimate`/
`confidence_score` threshold gate `tasks.py` already applies as ITS OWN
first trust checkpoint, via `opportunity_monitoring.
verify_change_candidate(source_trust="ai_reextraction", ...)`. A
re-extraction that doesn't clear that gate is HOLD -- nothing about the
opportunity is written, and the observed difference is only logged, not
recorded as history or propagated to any user (Part D/R).

Opportunities with no `raw_extracted_data` (nothing this call can
re-verify against) are skipped, not treated as an error.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.opportunity import Opportunity
from app.services.ai import ai_gateway
from app.services.opportunity_monitoring_pipeline import MonitoringCycleResult, process_opportunity_candidate
from app.workers.tasks import _coerce_academic_levels, _coerce_gender, _parse_deadline

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class ReverificationOutcome:
    opportunity_id: object
    skipped_no_raw_data: bool
    ai_call_failed: bool
    cycle_result: MonitoringCycleResult | None


async def reverify_opportunity(db: AsyncSession, *, opportunity: Opportunity) -> ReverificationOutcome:
    """One opportunity, one AI re-extraction, one full monitoring cycle.
    Never raises on a failed AI call -- a `ReverificationOutcome` with
    `ai_call_failed=True` is the caller's signal to count it as a source
    failure (Part Q's own `source_failures` observability counter), NOT
    as an opportunity-state change (Part Q's own explicit "source
    failure is not opportunity change" rule: a failed re-verification
    call leaves the opportunity completely untouched).

    Stamps `Opportunity.monitoring_last_checked_at` on EVERY exit path
    (skipped/failed/succeeded) -- Part P's own scheduler
    (`opportunity_monitoring_scheduler.find_opportunities_to_monitor`)
    reads this to make forward progress through the catalog; it is pure
    operational bookkeeping, never opportunity content, so touching it
    on a failed attempt does not violate Part Q."""
    now = dt.datetime.now(dt.timezone.utc)
    opportunity.monitoring_last_checked_at = now
    db.add(opportunity)

    if not opportunity.raw_extracted_data:
        await db.commit()
        return ReverificationOutcome(
            opportunity_id=opportunity.id, skipped_no_raw_data=True, ai_call_failed=False, cycle_result=None,
        )

    try:
        verification, _embedding = await ai_gateway.verify_and_embed(opportunity.raw_extracted_data)
    except Exception:
        logger.exception("opportunity_reverification: AI re-verification call failed for opportunity %s", opportunity.id)
        await db.commit()
        return ReverificationOutcome(
            opportunity_id=opportunity.id, skipped_no_raw_data=False, ai_call_failed=True, cycle_result=None,
        )

    candidate = {
        "deadline": _parse_deadline(verification.deadline, opportunity.id),
        "eligibility_criteria": verification.eligibility_criteria,
        "funding_details": verification.funding_details,
        "min_age": verification.min_age,
        "max_age": verification.max_age,
        "target_gender": _coerce_gender(verification.target_gender, opportunity.id),
        "target_academic_levels": _coerce_academic_levels(verification.target_academic_levels, opportunity.id),
    }
    # Coerce enum-typed candidates to their plain values so
    # `opportunity_monitoring`'s comparison/persistence logic (Part B)
    # sees the same plain-text shape `OpportunityChangeHistory.old_value`/
    # `new_value` already store for every other field -- matches
    # `change_history_service`'s own established "plain-text posture"
    # (see that module's own docstring).
    if candidate["target_gender"] is not None:
        candidate["target_gender"] = candidate["target_gender"].value
    if candidate["target_academic_levels"] is not None:
        candidate["target_academic_levels"] = [level.value for level in candidate["target_academic_levels"]]

    cycle_result = await process_opportunity_candidate(
        db,
        opportunity=opportunity,
        candidate=candidate,
        source_trust="ai_reextraction",
        change_reason=f"opportunity_reverification: scheduled re-check for opportunity {opportunity.id}.",
        ai_is_legitimate=verification.is_legitimate,
        ai_confidence_score=verification.confidence_score,
    )
    await db.commit()

    return ReverificationOutcome(
        opportunity_id=opportunity.id, skipped_no_raw_data=False, ai_call_failed=False, cycle_result=cycle_result,
    )
