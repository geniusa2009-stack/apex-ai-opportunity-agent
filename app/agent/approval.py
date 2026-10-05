"""Human approval -- the only way anything the agent produces becomes
visible or gets published. Version 1 has NO auto-publish path.

* approve          WAITING_APPROVAL -> APPROVED; the opportunity becomes
                   visible in the app (review_status=published); a staged
                   update proposal is applied with change history; the job
                   moves to PUBLISHING (idempotent publishers).
* reject           -> REJECTED; a newly created opportunity is marked
                   rejected (an existing live one is left untouched).
* request_revision -> REVISION_REQUESTED; the job goes back to WRITING with
                   the reviewer's notes (a new draft version follows).
* continue / discard for jobs parked in NEEDS_REVIEW.

Every decision is written to the existing moderation activity log
(OPPORTUNITY_APPROVE / OPPORTUNITY_REJECT) and to the job's own event
trail, and feeds the source's reputation (bounded, see reputation.py).
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import jobs
from app.agent.errors import AgentError, ErrorCategory
from app.agent.extraction import ExtractedOpportunity
from app.agent.opportunity_store import apply_update_proposal
from app.agent.reputation import TrustEvent, updated_trust
from app.agent.writer import PostFacts, fact_guard
from app.models.agent import AgentPostDraft, PipelineJob, SourceMessage
from app.models.agent_enums import DraftStatus, JobOutcome
from app.models.agent_enums import PipelineJobStatus as S
from app.models.enums import (
    AdminRole,
    ModerationAction,
    ModerationTargetType,
    OpportunityReviewStatus,
)
from app.models.opportunity import Opportunity
from app.models.source import Source
from app.services import moderation_log_service


class ApprovalError(AgentError):
    category = ErrorCategory.DATA


async def _job_for(db: AsyncSession, draft: AgentPostDraft) -> PipelineJob:
    job = await db.get(PipelineJob, draft.job_id) if draft.job_id else None
    if job is None:
        raise ApprovalError("draft has no pipeline job", code="DRAFT_WITHOUT_JOB")
    return job


async def _source_for(db: AsyncSession, job: PipelineJob) -> Source | None:
    if not job.source_message_id:
        return None
    message = await db.get(SourceMessage, job.source_message_id)
    return await db.get(Source, message.source_id) if message else None


async def _opportunity(db: AsyncSession, opportunity_id: uuid.UUID) -> Opportunity:
    opportunity = await db.get(Opportunity, opportunity_id)
    if opportunity is None:
        raise ApprovalError("opportunity not found", code="OPPORTUNITY_NOT_FOUND")
    return opportunity


def _require_waiting(draft: AgentPostDraft) -> None:
    if draft.status != DraftStatus.WAITING_APPROVAL.value:
        raise ApprovalError(f"draft is {draft.status}, not waiting for approval", code="DRAFT_NOT_WAITING")


def _log(db: AsyncSession, *, actor_id, actor_role, action, opportunity_id, outcome: dict) -> None:
    moderation_log_service.log_action(
        db,
        actor_id=actor_id,
        actor_role=actor_role,
        action=action,
        target_type=ModerationTargetType.OPPORTUNITY,
        target_id=opportunity_id,
        outcome={"source": "apex_agent", **outcome},
    )


async def approve_draft(
    db: AsyncSession,
    draft: AgentPostDraft,
    *,
    actor_id: uuid.UUID,
    actor_role: AdminRole | None,
    notes: str | None = None,
    edited_body: str | None = None,
) -> dict:
    """Approve (optionally with a human-edited body). Returns warnings from
    the fact guard on an edited body -- the human decides, but sees them."""
    _require_waiting(draft)
    job = await _job_for(db, draft)
    warnings: list[str] = []
    if edited_body is not None and edited_body.strip() and edited_body.strip() != draft.body:
        if draft.job_id:
            message = await db.get(SourceMessage, job.source_message_id) if job.source_message_id else None
            if message and message.extraction:
                guard = fact_guard(edited_body, PostFacts(ExtractedOpportunity.model_validate(message.extraction)))
                warnings = guard.violations
        draft.body = edited_body.strip()
        draft.fact_guard = {"ok": not warnings, "violations": warnings, "edited_by_human": True}

    now = dt.datetime.now(dt.timezone.utc)
    draft.status = DraftStatus.APPROVED.value
    draft.decided_by = actor_id
    draft.decided_at = now
    draft.decision_notes = notes
    # Any other open draft for the same opportunity is now obsolete.
    await db.execute(
        update(AgentPostDraft)
        .where(
            AgentPostDraft.opportunity_id == draft.opportunity_id,
            AgentPostDraft.id != draft.id,
            AgentPostDraft.status.in_([DraftStatus.DRAFT.value, DraftStatus.WAITING_APPROVAL.value]),
        )
        .values(status=DraftStatus.SUPERSEDED.value)
    )

    opportunity = await _opportunity(db, draft.opportunity_id)
    proposal = (job.payload or {}).get("update_proposal")
    changed: list[str] = []
    if proposal:
        message = await db.get(SourceMessage, job.source_message_id) if job.source_message_id else None
        changed = await apply_update_proposal(
            db,
            opportunity=opportunity,
            proposal=proposal,
            source_message_id=job.source_message_id,
            source_id=message.source_id if message else None,
            extraction=ExtractedOpportunity.model_validate(message.extraction) if message and message.extraction else None,
        )
    opportunity.review_status = OpportunityReviewStatus.PUBLISHED

    source = await _source_for(db, job)
    if source is not None:
        source.trust_score = updated_trust(source.trust_score, TrustEvent.HUMAN_APPROVED)

    _log(db, actor_id=actor_id, actor_role=actor_role, action=ModerationAction.OPPORTUNITY_APPROVE,
         opportunity_id=opportunity.id,
         outcome={"draft_id": str(draft.id), "version": draft.version, "notes": notes, "updated_fields": changed,
                  "edited": edited_body is not None})
    await jobs.transition(db, job, S.PUBLISHING, stage="human_approved",
                          detail={"actor_id": str(actor_id), "draft_id": str(draft.id), "warnings": warnings[:5]})
    await db.flush()
    return {"warnings": warnings, "updated_fields": changed}


async def reject_draft(
    db: AsyncSession,
    draft: AgentPostDraft,
    *,
    actor_id: uuid.UUID,
    actor_role: AdminRole | None,
    reason: str,
    inaccurate_source: bool = False,
) -> None:
    _require_waiting(draft)
    job = await _job_for(db, draft)
    draft.status = DraftStatus.REJECTED.value
    draft.decided_by = actor_id
    draft.decided_at = dt.datetime.now(dt.timezone.utc)
    draft.decision_notes = reason
    opportunity = await _opportunity(db, draft.opportunity_id)
    if not (job.payload or {}).get("update_proposal"):
        # Newly created by the agent -> never shown. An existing live
        # opportunity whose UPDATE was rejected stays exactly as it was.
        opportunity.review_status = OpportunityReviewStatus.REJECTED
    if inaccurate_source:
        source = await _source_for(db, job)
        if source is not None:
            source.trust_score = updated_trust(source.trust_score, TrustEvent.HUMAN_REJECTED_INACCURATE)
    _log(db, actor_id=actor_id, actor_role=actor_role, action=ModerationAction.OPPORTUNITY_REJECT,
         opportunity_id=opportunity.id,
         outcome={"draft_id": str(draft.id), "reason": reason, "inaccurate_source": inaccurate_source})
    await jobs.transition(db, job, S.COMPLETED, stage="human_rejected", outcome=JobOutcome.REJECTED_BY_HUMAN.value,
                          detail={"actor_id": str(actor_id), "reason": reason[:300]})
    await db.flush()


async def request_revision(db: AsyncSession, draft: AgentPostDraft, *, actor_id: uuid.UUID, notes: str) -> None:
    _require_waiting(draft)
    job = await _job_for(db, draft)
    draft.status = DraftStatus.REVISION_REQUESTED.value
    draft.decided_by = actor_id
    draft.decided_at = dt.datetime.now(dt.timezone.utc)
    draft.decision_notes = notes
    job.payload = {**(job.payload or {}), "revision_notes": [notes[:1000]], "human_revision": True}
    await jobs.transition(db, job, S.WRITING, stage="human_requested_revision",
                          detail={"actor_id": str(actor_id), "notes": notes[:300]})
    await db.flush()


async def continue_job(db: AsyncSession, job: PipelineJob, *, actor_id: uuid.UUID, notes: str | None = None) -> None:
    """A human looked at a NEEDS_REVIEW job (e.g. verification could not
    confirm the facts) and decides the agent may draft a post anyway. The
    draft still needs normal approval afterwards."""
    if job.status != S.NEEDS_REVIEW.value:
        raise ApprovalError(f"job is {job.status}, not needs_review", code="JOB_NOT_IN_REVIEW")
    if job.opportunity_id is None:
        # Failed before an opportunity existed: restart from where it stopped.
        await jobs.requeue(db, job)
        return
    job.payload = {**(job.payload or {}), "verification_overridden_by": str(actor_id)}
    existing_draft = await db.get(AgentPostDraft, job.draft_id) if job.draft_id else None
    target = S.WAITING_APPROVAL if existing_draft is not None else S.WRITING
    if existing_draft is not None:
        existing_draft.status = DraftStatus.WAITING_APPROVAL.value
    await jobs.transition(db, job, target, stage="human_continued",
                          detail={"actor_id": str(actor_id), "notes": (notes or "")[:300]})
    await db.flush()


async def discard_job(db: AsyncSession, job: PipelineJob, *, actor_id: uuid.UUID, reason: str) -> None:
    if job.status not in (S.NEEDS_REVIEW.value, S.WAITING_APPROVAL.value, S.FAILED.value, S.DEAD_LETTER.value):
        raise ApprovalError(f"job is {job.status}; cannot discard", code="JOB_NOT_DISCARDABLE")
    if job.opportunity_id and not (job.payload or {}).get("update_proposal"):
        opportunity = await db.get(Opportunity, job.opportunity_id)
        if opportunity is not None and opportunity.review_status != OpportunityReviewStatus.PUBLISHED:
            opportunity.review_status = OpportunityReviewStatus.REJECTED
    drafts = (await db.execute(select(AgentPostDraft).where(AgentPostDraft.job_id == job.id))).scalars().all()
    for draft in drafts:
        if draft.status in (DraftStatus.DRAFT.value, DraftStatus.WAITING_APPROVAL.value):
            draft.status = DraftStatus.REJECTED.value
    current = S(job.status)
    job.status = S.COMPLETED.value
    job.outcome = JobOutcome.REJECTED_BY_HUMAN.value
    job.completed_at = dt.datetime.now(dt.timezone.utc)
    job.locked_by = job.locked_until = job.next_retry_at = None
    await jobs.record_event(db, job, from_status=current.value, to_status=S.COMPLETED.value, stage="human_discarded",
                            detail={"actor_id": str(actor_id), "reason": reason[:300]})
    await db.flush()
