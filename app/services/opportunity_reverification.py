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

FULL INTELLIGENCE AUDIT FIX (found via this repo's own full-system
audit, not part of the original Opportunity Monitoring phase): earlier
versions of this module recorded a VERIFIED, HIGH/CRITICAL-severity
`OpportunityChangeHistory` row and even a user notification for a
changed field, but never actually wrote the new value onto the
`Opportunity` row itself. That meant the opportunity's own displayed
deadline/eligibility/funding/age/gender/academic-level data silently
never updated -- permanently stale relative to its own recorded
history -- and, because every subsequent scheduled re-check still
compared against that same stale value, it would keep re-detecting the
"same" real-world change and firing a fresh history row and
notification every cycle forever (a real, user-facing duplicate-alert
bug, not merely an internal inconsistency). This module now writes each
VERIFIED, actually-detected field's new value onto `opportunity` itself
(`_apply_verified_candidate_to_opportunity`, below) -- mirroring the
exact pattern `aggregator_service.upsert_opportunity`'s own
"structured" trust path already uses for the fields IT tracks. A HOLD
verification (low AI confidence) still writes nothing at all, per Part
D/R -- this fix only ever applies a candidate that already cleared the
VERIFY gate.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import VerificationDecisionStatus
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

    # Two representations of the same re-extracted data, kept
    # deliberately separate: `typed_values` holds the REAL, correctly-
    # typed value for each field (a `datetime`, a `Gender` enum, a
    # `list[AcademicLevel]`) -- what actually gets assigned onto the
    # `Opportunity` ORM object below. `candidate` (passed to the
    # DETECT/VERIFY/RECORD pipeline) coerces the same two enum-typed
    # fields to their plain `.value` text, exactly as before this fix --
    # `record_change_if_meaningful` stores `str(value)` verbatim
    # (`change_history_service.py`), and `str(Gender.MALE)` is the ugly
    # `"Gender.MALE"`, not `"male"`; keeping this coercion for the
    # comparison/history/notification path avoids reintroducing that
    # display bug while still letting the ORM assignment below use the
    # real, correctly-typed enum value.
    typed_values = {
        "deadline": _parse_deadline(verification.deadline, opportunity.id),
        "eligibility_criteria": verification.eligibility_criteria,
        "funding_details": verification.funding_details,
        "min_age": verification.min_age,
        "max_age": verification.max_age,
        "target_gender": _coerce_gender(verification.target_gender, opportunity.id),
        "target_academic_levels": _coerce_academic_levels(verification.target_academic_levels, opportunity.id),
    }
    candidate = dict(typed_values)
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
        source_id=opportunity.source_id,
        ai_is_legitimate=verification.is_legitimate,
        ai_confidence_score=verification.confidence_score,
    )

    # FULL INTELLIGENCE AUDIT FIX: only when the candidate actually
    # cleared VERIFY (never on HOLD -- Part D/R) do we write the new,
    # real value for each field DETECT found genuinely different onto
    # the `Opportunity` row itself -- mirroring `aggregator_service.
    # upsert_opportunity`'s own "structured" trust path, which already
    # does this for the fields IT tracks. This intentionally applies
    # every DETECTED field, not just the ones RECORD kept (an
    # IGNORE-severity change, e.g. a deadline shift of 0 real days, is
    # still a real, verified value worth keeping the opportunity's own
    # column in sync with -- IGNORE only means "don't bother the user
    # about it", never "don't trust it").
    if cycle_result.verification_status == VerificationDecisionStatus.VERIFIED:
        for change in cycle_result.detected:
            setattr(opportunity, change.field_name, typed_values[change.field_name])
        db.add(opportunity)

    await db.commit()

    return ReverificationOutcome(
        opportunity_id=opportunity.id, skipped_no_raw_data=False, ai_call_failed=False, cycle_result=cycle_result,
    )
