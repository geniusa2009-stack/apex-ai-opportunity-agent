"""Opportunity Monitoring & Personal Impact Engine, Part A: the one real
orchestration point that wires the composable DETECT -> VERIFY ->
CLASSIFY -> RECORD -> IMPACT -> NOTIFICATION stages together, so
neither caller (`aggregator_service.upsert_opportunity`,
`opportunity_reverification.py`'s Celery task) has to know the internal
order of six different service modules.

GUIDANCE (Part L) deliberately has no call site here: `guidance_
service.py`'s own generators already read `OpportunityChangeHistory` at
GUIDANCE-REQUEST time (the exact same read-time pattern the prior
User Intelligence + Proactive Guidance phase established for the
deadline-only case) -- once this module's RECORD stage writes a row,
every existing and newly-added guidance generator picks it up on the
user's next `GET /users/me/guidance` call with no push step needed.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.opportunity import Opportunity
from app.services.opportunity_change_notifications import create_change_notifications_for_opportunity
from app.services.opportunity_monitoring import (
    DetectedChange,
    RecordedChange,
    detect_meaningful_changes,
    record_verified_changes,
    verify_change_candidate,
)


@dataclasses.dataclass
class MonitoringCycleResult:
    detected: list[DetectedChange]
    recorded: list[RecordedChange]
    notifications_created: int


async def process_opportunity_candidate(
    db: AsyncSession,
    *,
    opportunity: Opportunity,
    candidate: dict,
    source_trust: str,
    change_reason: str,
    source_id: uuid.UUID | None = None,
    evidence_id: uuid.UUID | None = None,
    ai_is_legitimate: bool | None = None,
    ai_confidence_score: float | None = None,
    now: dt.datetime | None = None,
) -> MonitoringCycleResult:
    """One full monitoring cycle for one opportunity against one fresh
    `candidate` dict. Never raises on a "nothing changed"/"nothing
    verified"/"nothing notifiable" outcome -- those are the overwhelming
    common case (Part U's own `changes_detected`/`changes_verified`/
    `changes_rejected` observability counters are exactly what a caller
    derives from this result's own field lengths, logged by the caller
    per Part U rather than by this pure-ish orchestration function).
    """
    detected = detect_meaningful_changes(opportunity, candidate)
    if not detected:
        return MonitoringCycleResult(detected=[], recorded=[], notifications_created=0)

    verification_status = verify_change_candidate(
        source_trust=source_trust, ai_is_legitimate=ai_is_legitimate, ai_confidence_score=ai_confidence_score,
    )

    recorded = await record_verified_changes(
        db,
        opportunity_id=opportunity.id,
        changes=detected,
        verification_status=verification_status,
        change_reason=change_reason,
        source_id=source_id,
        evidence_id=evidence_id,
    )

    notifications_created = 0
    if recorded:
        notifications_created = await create_change_notifications_for_opportunity(
            db, opportunity=opportunity, recorded_changes=recorded, now=now,
        )

    return MonitoringCycleResult(detected=detected, recorded=recorded, notifications_created=notifications_created)
