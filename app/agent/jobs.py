"""Persistent, lease-based pipeline job primitives.

State lives ONLY in the `pipeline_jobs` table:

* A worker *claims* runnable jobs by writing `locked_by`/`locked_until`
  (a lease). On Postgres the claim uses ``FOR UPDATE SKIP LOCKED`` so two
  workers never claim the same row.
* If the worker dies mid-stage, nothing is lost: once the lease expires
  the job is runnable again and resumes from its current `status` (each
  stage is written to be idempotent -- see app/agent/pipeline.py).
* Failures are classified (app/agent/errors.py). Retryable ones schedule
  `next_retry_at` with capped exponential backoff and stay at the failing
  stage; after `max_attempts` the job goes to DEAD_LETTER. Validation/data
  problems go to NEEDS_REVIEW; configuration/auth problems go to FAILED.
* Every transition appends a `PipelineJobEvent` row (audit trail).

There is no unbounded loop anywhere: attempts are capped by
`max_attempts` (itself bounded by config validation).
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import socket
import uuid
from collections.abc import Iterable
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.errors import (
    NEEDS_REVIEW_CATEGORIES,
    RETRYABLE_CATEGORIES,
    AgentError,
    ErrorCategory,
)
from app.agent.observability import log_stage
from app.core.config import settings
from app.models.agent import PipelineJob, PipelineJobEvent
from app.models.agent_enums import PipelineJobStatus as S
from app.models.agent_enums import PipelineJobType

logger = logging.getLogger(__name__)

# Stages a worker actively executes. A job in one of these with no live
# lease is "in flight but abandoned" and is reclaimed.
WORK_STATUSES: frozenset[S] = frozenset(
    {S.QUEUED, S.PROCESSING, S.EXTRACTING, S.VERIFYING, S.WRITING, S.REVIEWING, S.PUBLISHING}
)
# States that wait for a human or are final. Never claimed by a worker.
PARKED_STATUSES: frozenset[S] = frozenset({S.WAITING_APPROVAL, S.NEEDS_REVIEW})
TERMINAL_STATUSES: frozenset[S] = frozenset({S.COMPLETED, S.FAILED, S.DEAD_LETTER})

_FAILURE_TARGETS = {S.FAILED, S.DEAD_LETTER, S.NEEDS_REVIEW}

# Allowed forward transitions. Retries stay in the same status (no
# transition), so they are not listed. Any stage may fail.
ALLOWED_TRANSITIONS: dict[S, frozenset[S]] = {
    # QUEUED may jump to any work stage: a manual requeue resumes where the
    # job stopped (payload['resume_at']).
    S.QUEUED: frozenset({S.PROCESSING, S.EXTRACTING, S.VERIFYING, S.WRITING, S.REVIEWING, S.PUBLISHING}),
    S.PROCESSING: frozenset({S.EXTRACTING, S.COMPLETED}),
    S.EXTRACTING: frozenset({S.VERIFYING, S.COMPLETED}),
    S.VERIFYING: frozenset({S.WRITING, S.COMPLETED}),
    S.WRITING: frozenset({S.REVIEWING}),
    S.REVIEWING: frozenset({S.WRITING, S.WAITING_APPROVAL, S.COMPLETED}),
    S.WAITING_APPROVAL: frozenset({S.PUBLISHING, S.COMPLETED, S.WRITING}),
    S.NEEDS_REVIEW: frozenset({S.QUEUED, S.WRITING, S.COMPLETED, S.WAITING_APPROVAL}),
    S.PUBLISHING: frozenset({S.COMPLETED}),
    S.COMPLETED: frozenset(),
    S.FAILED: frozenset({S.QUEUED}),  # manual requeue only
    S.DEAD_LETTER: frozenset({S.QUEUED}),  # manual requeue only
}


class InvalidTransitionError(AgentError):
    category = ErrorCategory.SYSTEM

    def __init__(self, current: str, target: str):
        super().__init__(f"illegal job transition {current} -> {target}", code="INVALID_JOB_TRANSITION")


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    """SQLite returns naive datetimes; treat them as UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def backoff_seconds(attempts: int, *, base: int | None = None, cap: int | None = None) -> int:
    """Capped exponential backoff: base * 2**(attempts-1), never above cap.
    Deterministic (no jitter) so behaviour is reproducible and testable."""
    base = base if base is not None else settings.agent_job_backoff_base_seconds
    cap = cap if cap is not None else settings.agent_job_backoff_max_seconds
    exponent = max(attempts - 1, 0)
    return int(min(cap, base * (2**min(exponent, 30))))


async def record_event(
    db: AsyncSession,
    job: PipelineJob,
    *,
    from_status: str | None,
    to_status: str,
    stage: str | None = None,
    detail: dict[str, Any] | None = None,
    duration_ms: int | None = None,
) -> None:
    db.add(
        PipelineJobEvent(
            job_id=job.id,
            from_status=from_status,
            to_status=to_status,
            stage=stage,
            detail=detail,
            duration_ms=duration_ms,
        )
    )


async def create_job(
    db: AsyncSession,
    *,
    job_type: PipelineJobType = PipelineJobType.PROCESS_MESSAGE,
    source_message_id: uuid.UUID | None = None,
    opportunity_id: uuid.UUID | None = None,
    draft_id: uuid.UUID | None = None,
    priority: int = 0,
    payload: dict[str, Any] | None = None,
    max_attempts: int | None = None,
) -> PipelineJob:
    job = PipelineJob(
        job_type=job_type.value,
        status=S.QUEUED.value,
        source_message_id=source_message_id,
        opportunity_id=opportunity_id,
        draft_id=draft_id,
        priority=priority,
        payload=payload or {},
        attempts=0,
        max_attempts=max_attempts or settings.agent_job_max_attempts,
        correlation_id=uuid.uuid4().hex,
    )
    db.add(job)
    await db.flush()
    await record_event(db, job, from_status=None, to_status=S.QUEUED.value, stage="created")
    log_stage("job_created", job_id=job.id, correlation_id=job.correlation_id, status=job.status)
    return job


async def transition(
    db: AsyncSession,
    job: PipelineJob,
    target: S,
    *,
    stage: str | None = None,
    detail: dict[str, Any] | None = None,
    duration_ms: int | None = None,
    outcome: str | None = None,
) -> None:
    current = S(job.status)
    if target not in ALLOWED_TRANSITIONS[current] and target not in _FAILURE_TARGETS:
        raise InvalidTransitionError(current.value, target.value)
    job.status = target.value
    if outcome is not None:
        job.outcome = outcome
    if target in TERMINAL_STATUSES or target in PARKED_STATUSES:
        job.locked_by = None
        job.locked_until = None
        job.next_retry_at = None
    if target in TERMINAL_STATUSES:
        job.completed_at = utcnow()
    # Remember the last real work stage so a manual requeue can resume there.
    if current in WORK_STATUSES and current != S.QUEUED:
        job.payload = {**(job.payload or {}), "last_stage": current.value}
    await record_event(
        db, job, from_status=current.value, to_status=target.value, stage=stage, detail=detail, duration_ms=duration_ms
    )
    log_stage(
        stage or target.value,
        job_id=job.id,
        correlation_id=job.correlation_id,
        source_message_id=job.source_message_id,
        opportunity_id=job.opportunity_id,
        status=target.value,
        duration_ms=duration_ms,
        outcome=outcome,
    )


def runnable_filter(now: dt.datetime):
    return and_(
        PipelineJob.status.in_([s.value for s in WORK_STATUSES]),
        or_(PipelineJob.next_retry_at.is_(None), PipelineJob.next_retry_at <= now),
        or_(PipelineJob.locked_until.is_(None), PipelineJob.locked_until < now),
    )


async def claim_due_jobs(
    db: AsyncSession,
    *,
    limit: int | None = None,
    owner: str | None = None,
    now: dt.datetime | None = None,
    lease_seconds: int | None = None,
    job_types: Iterable[PipelineJobType] | None = None,
) -> list[PipelineJob]:
    """Atomically lease up to `limit` runnable jobs (highest priority, oldest
    first). Includes jobs whose previous worker crashed (expired lease)."""
    now = now or utcnow()
    owner = owner or worker_id()
    lease = dt.timedelta(seconds=lease_seconds or settings.agent_job_lease_seconds)
    query = (
        select(PipelineJob)
        .where(runnable_filter(now))
        .order_by(PipelineJob.priority.desc(), PipelineJob.created_at.asc())
        .limit(limit or settings.agent_job_batch_size)
    )
    if job_types:
        query = query.where(PipelineJob.job_type.in_([t.value for t in job_types]))
    if db.bind is not None and db.bind.dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    jobs = list((await db.execute(query)).scalars().all())
    for job in jobs:
        recovered = job.locked_until is not None  # had a lease that expired -> crash recovery
        job.locked_by = owner
        job.locked_until = now + lease
        if job.started_at is None:
            job.started_at = now
        if recovered:
            await record_event(
                db, job, from_status=job.status, to_status=job.status, stage="lease_recovered", detail={"owner": owner}
            )
            log_stage("lease_recovered", job_id=job.id, correlation_id=job.correlation_id, status=job.status)
    await db.flush()
    return jobs


async def renew_lease(db: AsyncSession, job: PipelineJob, *, now: dt.datetime | None = None) -> None:
    now = now or utcnow()
    job.locked_until = now + dt.timedelta(seconds=settings.agent_job_lease_seconds)
    await db.flush()


async def release(db: AsyncSession, job: PipelineJob) -> None:
    """Drop the lease after a stage run finished without parking/terminating
    (e.g. the job moved to its next work stage and will be picked up again)."""
    job.locked_by = None
    job.locked_until = None
    await db.flush()


async def handle_failure(
    db: AsyncSession, job: PipelineJob, error: AgentError, *, now: dt.datetime | None = None
) -> S:
    """Apply the retry policy for `error` and return the resulting status.

    - retryable categories: attempts += 1; schedule a retry at the SAME
      stage, or DEAD_LETTER once attempts reach max_attempts
    - validation/data: NEEDS_REVIEW (human), no retry
    - authentication/config: FAILED, no retry
    """
    now = now or utcnow()
    job.last_error = error.message[:4000]
    job.last_error_code = error.code
    job.last_error_category = error.category.value
    detail = {"error_code": error.code, "category": error.category.value}

    if error.category in RETRYABLE_CATEGORIES:
        job.attempts = min(job.attempts + 1, job.max_attempts)
        if job.attempts >= job.max_attempts:
            await transition(db, job, S.DEAD_LETTER, stage="dead_letter", detail={**detail, "attempts": job.attempts})
            log_stage(
                "dead_letter",
                job_id=job.id,
                correlation_id=job.correlation_id,
                status=S.DEAD_LETTER.value,
                error_code=error.code,
                level=logging.ERROR,
            )
            return S.DEAD_LETTER
        delay = backoff_seconds(job.attempts)
        job.next_retry_at = now + dt.timedelta(seconds=delay)
        job.locked_by = None
        job.locked_until = None
        await record_event(
            db,
            job,
            from_status=job.status,
            to_status=job.status,
            stage="retry_scheduled",
            detail={**detail, "attempts": job.attempts, "retry_in_seconds": delay},
        )
        log_stage(
            "retry_scheduled",
            job_id=job.id,
            correlation_id=job.correlation_id,
            status=job.status,
            error_code=error.code,
            attempts=job.attempts,
            retry_in_seconds=delay,
            level=logging.WARNING,
        )
        return S(job.status)

    target = S.NEEDS_REVIEW if error.category in NEEDS_REVIEW_CATEGORIES else S.FAILED
    await transition(db, job, target, stage="failed", detail=detail)
    log_stage(
        "job_failed",
        job_id=job.id,
        correlation_id=job.correlation_id,
        status=target.value,
        error_code=error.code,
        level=logging.ERROR if target == S.FAILED else logging.WARNING,
    )
    return target


async def requeue(db: AsyncSession, job: PipelineJob, *, reset_attempts: bool = True) -> None:
    """Manual (admin) requeue of a FAILED/DEAD_LETTER/NEEDS_REVIEW job. It
    resumes at the last work stage it reached."""
    current = S(job.status)
    if current not in (S.FAILED, S.DEAD_LETTER, S.NEEDS_REVIEW):
        raise InvalidTransitionError(current.value, S.QUEUED.value)
    resume_at = (job.payload or {}).get("last_stage")
    if reset_attempts:
        job.attempts = 0
    job.next_retry_at = None
    job.locked_by = None
    job.locked_until = None
    job.completed_at = None
    job.status = S.QUEUED.value
    if resume_at:
        job.payload = {**(job.payload or {}), "resume_at": resume_at}
    await record_event(db, job, from_status=current.value, to_status=S.QUEUED.value, stage="manual_requeue",
                       detail={"resume_at": resume_at})


async def get_job(db: AsyncSession, job_id: uuid.UUID) -> PipelineJob | None:
    return await db.get(PipelineJob, job_id)
