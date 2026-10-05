"""Agent metrics computed from the database (not from process memory), so
they are correct across API instances, workers and restarts.

opportunities_received   source messages ingested
opportunities_rejected   jobs completed as not-an-opportunity / expired /
                         rejected by review or by a human
opportunities_verified   opportunities whose official page confirmed facts
duplicates_detected      messages resolved as duplicates (any layer)
ai_failures              LLM-related errors recorded on job events
verification_failures    jobs blocked/contradicted at verification
publications_successful  publications in PUBLISHED
publications_failed      publications in FAILED or UNKNOWN
average_processing_time  mean seconds from job creation to completion
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import (
    AgentOpportunityFacts,
    PipelineJob,
    PipelineJobEvent,
    Publication,
    SourceMessage,
)
from app.models.agent_enums import (
    JobOutcome,
    PipelineJobStatus,
    PublicationStatus,
    SourceMessageStatus,
    VerificationLevel,
)

_REJECTED_OUTCOMES = [
    JobOutcome.NOT_OPPORTUNITY.value,
    JobOutcome.EXPIRED.value,
    JobOutcome.REJECTED_BY_REVIEW.value,
    JobOutcome.REJECTED_BY_HUMAN.value,
    JobOutcome.REJECTED_BY_VERIFICATION.value,
]
_LLM_CODE_PREFIXES = ("LLM_", "GEMINI_", "OPENROUTER_")
MAX_EVENTS_SCANNED = 50_000


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


async def _count(db: AsyncSession, query) -> int:
    return int((await db.execute(query)).scalar_one() or 0)


async def compute_metrics(db: AsyncSession, *, since: dt.datetime | None = None) -> dict[str, Any]:
    since = since or dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
    received = await _count(db, select(func.count()).select_from(SourceMessage).where(SourceMessage.created_at >= since))
    duplicates = await _count(
        db,
        select(func.count()).select_from(SourceMessage).where(
            SourceMessage.created_at >= since,
            (SourceMessage.processing_status == SourceMessageStatus.DUPLICATE_MESSAGE.value)
            | (SourceMessage.dedup_outcome == "duplicate"),
        ),
    )
    rejected = await _count(
        db,
        select(func.count()).select_from(PipelineJob).where(PipelineJob.created_at >= since,
                                                            PipelineJob.outcome.in_(_REJECTED_OUTCOMES)),
    )
    verified = await _count(
        db,
        select(func.count()).select_from(AgentOpportunityFacts).where(
            AgentOpportunityFacts.updated_at >= since,
            AgentOpportunityFacts.verification_level.in_(
                [VerificationLevel.OFFICIAL_CONFIRMED.value, VerificationLevel.PARTIALLY_CONFIRMED.value]
            ),
        ),
    )
    publication_rows = (
        await db.execute(
            select(Publication.status, func.count()).where(Publication.created_at >= since).group_by(Publication.status)
        )
    ).all()
    publications: dict[str, int] = {str(row[0]): int(row[1]) for row in publication_rows}

    events = (
        await db.execute(
            select(PipelineJobEvent.stage, PipelineJobEvent.detail)
            .where(PipelineJobEvent.created_at >= since)
            .limit(MAX_EVENTS_SCANNED)
        )
    ).all()
    ai_failures = 0
    verification_failures = 0
    for stage, detail in events:
        code = str((detail or {}).get("error_code", ""))
        if stage in ("retry_scheduled", "failed", "dead_letter") and code.startswith(_LLM_CODE_PREFIXES):
            ai_failures += 1
        if stage == "verification_blocked":
            verification_failures += 1

    durations = (
        await db.execute(
            select(PipelineJob.created_at, PipelineJob.completed_at).where(
                PipelineJob.created_at >= since, PipelineJob.status == PipelineJobStatus.COMPLETED.value,
                PipelineJob.completed_at.is_not(None),
            ).limit(MAX_EVENTS_SCANNED)
        )
    ).all()
    seconds = [
        (done_at - created_at).total_seconds()
        for created_at, done_at in ((_aware(c), _aware(d)) for c, d in durations)
        if created_at is not None and done_at is not None
    ]
    status_rows = (await db.execute(select(PipelineJob.status, func.count()).group_by(PipelineJob.status))).all()
    by_status: dict[str, int] = {str(row[0]): int(row[1]) for row in status_rows}
    return {
        "since": since.isoformat(),
        "opportunities_received": received,
        "opportunities_rejected": rejected,
        "opportunities_verified": verified,
        "duplicates_detected": duplicates,
        "ai_failures": ai_failures,
        "verification_failures": verification_failures,
        "publications_successful": int(publications.get(PublicationStatus.PUBLISHED.value, 0)),
        "publications_failed": int(publications.get(PublicationStatus.FAILED.value, 0))
        + int(publications.get(PublicationStatus.UNKNOWN.value, 0)),
        "publications_awaiting_manual": int(publications.get(PublicationStatus.AWAITING_MANUAL.value, 0)),
        "average_processing_time_seconds": round(sum(seconds) / len(seconds), 3) if seconds else None,
        "jobs_by_status": {str(k): int(v) for k, v in by_status.items()},
    }
