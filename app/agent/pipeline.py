"""Pipeline orchestration for one `PipelineJob`.

    QUEUED -> PROCESSING (triage, pre-extraction exact dedup, semantic candidates)
           -> EXTRACTING (1 LLM call + self-correction; full dedup decision;
                          create opportunity / stage an update proposal)
           -> VERIFYING  (official page + D6 claims/evidence)
           -> WRITING    (RAG examples + writer + fact guard)
           -> REVIEWING  (quality gate [+ optional LLM review], bounded revisions)
           -> WAITING_APPROVAL  (human)            [app/agent/approval.py]
           -> PUBLISHING (idempotent publishers)
           -> COMPLETED

Every stage is a checkpoint: its results are written to the database and
committed before the job moves on, and every stage is safe to re-run after
a crash (it first looks for its own previous results). A failure rolls back
the half-finished stage and is handed to the retry policy in
app/agent/jobs.py. LLM calls per message, typical case: 1 (extraction), +1
if the LLM writer is enabled, +1 if the LLM reviewer is enabled; chatter
and obvious re-posts cost 0.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent import jobs
from app.agent.dedup import SemanticSearch, decide, pre_extraction_duplicate
from app.agent.errors import AgentError, DataError, classify_exception
from app.agent.extraction import ExtractedOpportunity, extract_opportunity, triage
from app.agent.llm import LLMClient, build_embedding_provider, build_llm_client
from app.agent.llm.base import EmbeddingProvider
from app.agent.observability import log_stage, timed
from app.agent.opportunity_store import (
    compute_update_proposal,
    create_opportunity,
    dedup_facts_from,
    link_message,
    source_of_message,
)
from app.agent.publishing import PublisherConnector, build_publishers, publish_draft
from app.agent.rag import retrieve_style_examples
from app.agent.reputation import TrustEvent, updated_trust
from app.agent.review import review_post
from app.agent.verification import (
    HttpPageFetcher,
    NullPageFetcher,
    PageFetcher,
    verify_opportunity,
)
from app.agent.writer import PostFacts, write_post
from app.core.config import Settings
from app.core.config import settings as global_settings
from app.models.agent import (
    AgentOpportunityFacts,
    AgentPostDraft,
    PipelineJob,
    Publication,
    SourceMessage,
)
from app.models.agent_enums import (
    DedupOutcome,
    DraftStatus,
    JobOutcome,
    PublicationStatus,
    ReviewDecision,
    SourceMessageStatus,
    VerificationLevel,
)
from app.models.agent_enums import (
    PipelineJobStatus as S,
)
from app.models.enums import OpportunityReviewStatus
from app.models.opportunity import Opportunity


@dataclasses.dataclass
class AgentDependencies:
    llm: LLMClient
    fetcher: PageFetcher
    publishers: list[PublisherConnector]
    embedder: EmbeddingProvider | None = None
    semantic_search: SemanticSearch | None = None
    writer_mode: str = "template"
    reviewer_llm_enabled: bool = False
    max_revision_rounds: int = 2
    today: Callable[[], dt.date] = lambda: dt.datetime.now(dt.timezone.utc).date()


def build_dependencies(cfg: Settings | None = None) -> AgentDependencies:
    cfg = cfg or global_settings
    fetcher: PageFetcher = (
        HttpPageFetcher(timeout_seconds=cfg.agent_official_page_timeout_seconds, max_bytes=cfg.agent_official_page_max_bytes)
        if cfg.agent_official_page_fetch_enabled
        else NullPageFetcher()
    )
    return AgentDependencies(
        llm=build_llm_client(cfg),
        fetcher=fetcher,
        publishers=build_publishers(cfg),
        embedder=build_embedding_provider(cfg),
        semantic_search=None,  # see semantic_search_for(): only valid when models match
        writer_mode=cfg.agent_writer_mode,
        reviewer_llm_enabled=cfg.agent_reviewer_llm_enabled,
        max_revision_rounds=cfg.agent_max_revision_rounds,
    )


def semantic_search_for(db: AsyncSession, embedder: EmbeddingProvider | None, cfg: Settings | None = None) -> SemanticSearch | None:
    """pgvector candidate search over `opportunities.embedding`. Enabled only
    on Postgres AND when the agent's embedding model is the same model that
    produced the stored vectors (vectors from different models are not
    comparable). Candidates only -- `dedup.compare()` decides."""
    cfg = cfg or global_settings
    if embedder is None or db.bind is None or db.bind.dialect.name != "postgresql":
        return None
    if embedder.model != cfg.ai_embedding_model:
        return None

    async def search(facts) -> dict[uuid.UUID, float]:
        text = " ".join(filter(None, [facts.title, facts.organization, facts.eligibility, facts.funding]))
        if not text:
            return {}
        [vector] = await embedder.embed([text])
        distance = Opportunity.embedding.cosine_distance(vector)
        rows = await db.execute(
            select(Opportunity.id, (1 - distance).label("similarity"))
            .where(Opportunity.embedding.is_not(None), (1 - distance) >= cfg.agent_semantic_candidate_threshold)
            .order_by(distance.asc())
            .limit(cfg.agent_semantic_candidate_limit)
        )
        return {row[0]: float(row[1]) for row in rows.all()}

    return search


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

async def _opportunity(db: AsyncSession, opportunity_id: uuid.UUID | None) -> Opportunity:
    opportunity = await db.get(Opportunity, opportunity_id) if opportunity_id else None
    if opportunity is None:
        raise DataError("opportunity referenced by the job no longer exists", code="OPPORTUNITY_NOT_FOUND")
    return opportunity


async def _message(db: AsyncSession, job: PipelineJob) -> SourceMessage:
    message = await db.get(SourceMessage, job.source_message_id) if job.source_message_id else None
    if message is None:
        raise DataError("job has no source message", code="JOB_WITHOUT_MESSAGE")
    return message


def _extraction(message: SourceMessage) -> ExtractedOpportunity | None:
    return ExtractedOpportunity.model_validate(message.extraction) if message.extraction else None


def _payload(job: PipelineJob) -> dict[str, Any]:
    return dict(job.payload or {})


def _set_payload(job: PipelineJob, **values: Any) -> None:
    job.payload = {**(job.payload or {}), **values}


async def _complete(db: AsyncSession, job: PipelineJob, outcome: JobOutcome, *, stage: str, detail: dict | None = None,
                    duration_ms: int | None = None) -> None:
    await jobs.transition(db, job, S.COMPLETED, stage=stage, outcome=outcome.value, detail=detail, duration_ms=duration_ms)


def _merged_extraction(existing: dict[str, Any] | None, new: ExtractedOpportunity) -> ExtractedOpportunity:
    """Existing known facts overlaid by what the newer message states."""
    merged = dict(existing or {})
    for key, value in new.model_dump().items():
        if value not in (None, "", []):
            merged[key] = value
    merged["is_opportunity"] = True
    return ExtractedOpportunity.model_validate(merged)


async def _facts_for_job(db: AsyncSession, job: PipelineJob, message: SourceMessage) -> ExtractedOpportunity:
    extraction = _extraction(message)
    if extraction is None:
        raise DataError("no stored extraction for this job", code="MISSING_EXTRACTION")
    if _payload(job).get("update_proposal") and job.opportunity_id:
        row = await db.get(AgentOpportunityFacts, job.opportunity_id)
        return _merged_extraction(row.facts if row else None, extraction)
    return extraction


# --------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------

async def stage_processing(db: AsyncSession, job: PipelineJob, deps: AgentDependencies) -> None:
    message = await _message(db, job)
    with timed() as elapsed:
        result = triage(message.raw_text, message.links or [])
        if not result.needs_llm:
            message.processing_status = SourceMessageStatus.NOT_OPPORTUNITY.value
            await _complete(db, job, JobOutcome.NOT_OPPORTUNITY, stage="triage",
                            detail={"triage": result.decision.value, "reasons": list(result.reasons)},
                            duration_ms=elapsed())
            return
        duplicate = await pre_extraction_duplicate(db, links=message.links or [], text=message.raw_text,
                                                   content_hash=message.content_hash)
        if duplicate is not None:
            message.processing_status = SourceMessageStatus.PROCESSED.value
            message.dedup_outcome = duplicate.outcome.value
            message.dedup_details = duplicate.as_dict()
            opportunity = await _opportunity(db, duplicate.opportunity_id)
            await link_message(db, opportunity.id, message, "duplicate")
            job.opportunity_id = opportunity.id
            await _complete(db, job, JobOutcome.DUPLICATE, stage="exact_dedup", detail={"reasons": duplicate.reasons},
                            duration_ms=elapsed())
            return
        _set_payload(job, triage=result.decision.value)
        await jobs.transition(db, job, S.EXTRACTING, stage="normalized", detail={"triage": result.decision.value},
                              duration_ms=elapsed())


async def stage_extracting(db: AsyncSession, job: PipelineJob, deps: AgentDependencies) -> None:
    message = await _message(db, job)
    source = await source_of_message(db, message)
    with timed() as elapsed:
        extraction = _extraction(message)
        if extraction is None:  # not done before this (possibly resumed) run
            result = await extract_opportunity(deps.llm, text=message.raw_text, links=message.links or [],
                                               source_hint=f"{source.source_type} source")
            extraction = result.value
            message.extraction = extraction.model_dump()
            _set_payload(job, extraction_attempts=result.attempts, extraction_provider=result.provider)
        if not extraction.is_opportunity:
            message.processing_status = SourceMessageStatus.NOT_OPPORTUNITY.value
            await _complete(db, job, JobOutcome.NOT_OPPORTUNITY, stage="extraction",
                            detail={"reason": extraction.not_opportunity_reason}, duration_ms=elapsed())
            return

        if job.opportunity_id is None:
            facts = dedup_facts_from(extraction, content_hash=message.content_hash)
            semantic = deps.semantic_search or semantic_search_for(db, deps.embedder)
            decision = await decide(db, facts, semantic_search=semantic)
            message.dedup_outcome = decision.outcome.value
            message.dedup_details = decision.as_dict()

            if decision.outcome == DedupOutcome.DUPLICATE:
                existing = await _opportunity(db, decision.opportunity_id)
                await link_message(db, existing.id, message, "duplicate")
                message.processing_status = SourceMessageStatus.PROCESSED.value
                job.opportunity_id = existing.id
                await _complete(db, job, JobOutcome.DUPLICATE, stage="dedup", detail={"reasons": decision.reasons},
                                duration_ms=elapsed())
                return

            if decision.outcome == DedupOutcome.UPDATE_EXISTING:
                opportunity = await _opportunity(db, decision.opportunity_id)
                facts_row = await db.get(AgentOpportunityFacts, decision.opportunity_id)
                proposal = compute_update_proposal(opportunity, facts_row, extraction)
                await link_message(db, opportunity.id, message, "update")
                job.opportunity_id = opportunity.id
                if not proposal:
                    message.processing_status = SourceMessageStatus.PROCESSED.value
                    await _complete(db, job, JobOutcome.DUPLICATE, stage="dedup",
                                    detail={"reasons": decision.reasons + ["no field actually changed"]},
                                    duration_ms=elapsed())
                    return
                _set_payload(job, update_proposal=proposal)
            else:
                opportunity = await create_opportunity(db, extraction=extraction, message=message, source=source)
                job.opportunity_id = opportunity.id
                if decision.outcome == DedupOutcome.POSSIBLE_DUPLICATE:
                    _set_payload(job, duplicate_of=str(decision.opportunity_id))
            _set_payload(job, dedup_outcome=decision.outcome.value)
        message.processing_status = SourceMessageStatus.PROCESSED.value
        await jobs.transition(db, job, S.VERIFYING, stage="extracted",
                              detail={"dedup": _payload(job).get("dedup_outcome"),
                                      "missing": extraction.computed_missing_fields()},
                              duration_ms=elapsed())


async def stage_verifying(db: AsyncSession, job: PipelineJob, deps: AgentDependencies) -> None:
    message = await _message(db, job)
    source = await source_of_message(db, message)
    facts = await _facts_for_job(db, job, message)
    with timed() as elapsed:
        report = await verify_opportunity(
            db,
            opportunity_id=job.opportunity_id,
            extraction=facts,
            message_text=message.raw_text,
            source_id=source.id,
            source_type=source.source_type,
            trust_score=source.trust_score,
            fetcher=deps.fetcher,
            today=deps.today(),
        )
        _set_payload(job, verification=report.as_dict())
        if not _payload(job).get("update_proposal"):
            row = await db.get(AgentOpportunityFacts, job.opportunity_id)
            if row is not None:
                row.verification_level = report.level.value
                row.verification_report = report.as_dict()
        if report.level == VerificationLevel.CONTRADICTED:
            source.trust_score = updated_trust(source.trust_score, TrustEvent.VERIFICATION_CONTRADICTED)
        if report.expired:
            if not _payload(job).get("update_proposal"):
                opportunity = await _opportunity(db, job.opportunity_id)
                opportunity.review_status = OpportunityReviewStatus.REJECTED
            await _complete(db, job, JobOutcome.EXPIRED, stage="verification", detail={"level": report.level.value},
                            duration_ms=elapsed())
            return
        if report.blocking:
            await jobs.transition(db, job, S.NEEDS_REVIEW, stage="verification_blocked",
                                  detail={"level": report.level.value, "red_flags": report.red_flags[:5],
                                          "contradicted": report.contradicted_claims},
                                  duration_ms=elapsed())
            return
        await jobs.transition(db, job, S.WRITING, stage="verified", detail={"level": report.level.value},
                              duration_ms=elapsed())


def _post_facts(job: PipelineJob, facts: ExtractedOpportunity) -> PostFacts:
    verification = _payload(job).get("verification") or {}
    warnings = []
    if verification.get("level") == VerificationLevel.SOURCE_ONLY.value:
        warnings.append("لم نتمكن من التأكد من التفاصيل من الموقع الرسمي")
    return PostFacts(facts, verification_level=verification.get("level"), warnings=warnings)


async def stage_writing(db: AsyncSession, job: PipelineJob, deps: AgentDependencies) -> None:
    message = await _message(db, job)
    facts = await _facts_for_job(db, job, message)
    post_facts = _post_facts(job, facts)
    with timed() as elapsed:
        if job.draft_id:
            current = await db.get(AgentPostDraft, job.draft_id)
            if current is not None and current.status == DraftStatus.DRAFT.value and current.review_decision is None:
                # Resumed after a crash between writing and reviewing: reuse.
                await jobs.transition(db, job, S.REVIEWING, stage="written_resumed", duration_ms=elapsed())
                return
        examples: list[str] = []
        example_ids: list[str] = []
        if deps.writer_mode == "llm":
            found = await retrieve_style_examples(
                db,
                query_text=" ".join(filter(None, [facts.title, facts.description, facts.organization])),
                opportunity_type=facts.opportunity_type,
                exclude_opportunity_id=job.opportunity_id,
            )
            examples = [ex.body for ex in found]
            example_ids = [str(ex.draft_id) for ex in found]
        written = await write_post(
            post_facts,
            mode=deps.writer_mode,
            llm=deps.llm,
            examples=examples,
            revision_notes=_payload(job).get("revision_notes"),
            max_guard_rounds=max(1, deps.max_revision_rounds),
        )
        version = (
            await db.execute(select(func.max(AgentPostDraft.version)).where(AgentPostDraft.opportunity_id == job.opportunity_id))
        ).scalar() or 0
        draft = AgentPostDraft(
            opportunity_id=job.opportunity_id,
            job_id=job.id,
            version=version + 1,
            body=written.body,
            language=facts.language if facts.language in ("ar", "en", "fr") else "ar",
            writer_mode=written.writer_mode,
            style_example_ids=example_ids or None,
            fact_guard=written.guard.as_dict(),
            status=DraftStatus.DRAFT.value,
        )
        db.add(draft)
        await db.flush()
        job.draft_id = draft.id
        await jobs.transition(db, job, S.REVIEWING, stage="written",
                              detail={"writer_mode": written.writer_mode, "examples": len(examples),
                                      "version": draft.version},
                              duration_ms=elapsed())


async def stage_reviewing(db: AsyncSession, job: PipelineJob, deps: AgentDependencies) -> None:
    message = await _message(db, job)
    facts = await _facts_for_job(db, job, message)
    draft = await db.get(AgentPostDraft, job.draft_id)
    if draft is None:
        raise DataError("review without a draft", code="MISSING_DRAFT")
    payload = _payload(job)
    with timed() as elapsed:
        result = await review_post(
            draft.body,
            _post_facts(job, facts),
            verification_level=(
                "human_override" if payload.get("verification_overridden_by")
                else (payload.get("verification") or {}).get("level")
            ),
            red_flags=(payload.get("verification") or {}).get("red_flags"),
            dedup_outcome=payload.get("dedup_outcome"),
            duplicate_of=payload.get("duplicate_of"),
            llm=deps.llm,
            use_llm=deps.reviewer_llm_enabled,
        )
        draft.review_decision = result.decision.value
        draft.review_reasons = result.as_dicts()
        rounds = int(payload.get("revision_rounds", 0))
        detail = {"decision": result.decision.value, "issues": [i.code for i in result.issues][:10]}

        if result.decision == ReviewDecision.NEEDS_REVISION and deps.writer_mode == "llm" and rounds < deps.max_revision_rounds:
            draft.status = DraftStatus.SUPERSEDED.value
            _set_payload(job, revision_rounds=rounds + 1, revision_notes=result.revision_notes)
            await jobs.transition(db, job, S.WRITING, stage="revision_requested_by_reviewer", detail=detail,
                                  duration_ms=elapsed())
            return
        if result.decision == ReviewDecision.REJECT:
            await jobs.transition(db, job, S.NEEDS_REVIEW, stage="rejected_by_reviewer", detail=detail,
                                  duration_ms=elapsed())
            return
        # APPROVE, or NEEDS_REVISION with no rounds left: a human decides,
        # with every issue visible on the draft.
        draft.status = DraftStatus.WAITING_APPROVAL.value
        await jobs.transition(db, job, S.WAITING_APPROVAL, stage="ready_for_human", detail=detail,
                              outcome=JobOutcome.DRAFT_READY.value, duration_ms=elapsed())


async def stage_publishing(db: AsyncSession, job: PipelineJob, deps: AgentDependencies) -> None:
    draft = await db.get(AgentPostDraft, job.draft_id) if job.draft_id else None
    if draft is None or draft.status not in (DraftStatus.APPROVED.value, DraftStatus.PUBLISHED.value):
        raise DataError("only a human-approved draft can be published", code="DRAFT_NOT_APPROVED")
    opportunity = await db.get(Opportunity, draft.opportunity_id)
    facts_row = await db.get(AgentOpportunityFacts, draft.opportunity_id)
    link = (facts_row.application_url or facts_row.official_url) if facts_row else None
    link = link or (opportunity.target_url if opportunity else None)
    with timed() as elapsed:
        results = []
        for publisher in deps.publishers:
            outcome = await publish_draft(db, draft, publisher, title=opportunity.title if opportunity else "",
                                          link=link, commit=db.commit)
            results.append({"platform": publisher.name, "status": outcome.publication.status})
        statuses = [r["status"] for r in results]
        if statuses and all(s == PublicationStatus.PUBLISHED.value for s in statuses):
            draft.status = DraftStatus.PUBLISHED.value
        await _complete(db, job, JobOutcome.PUBLISHED, stage="published", detail={"publications": results},
                        duration_ms=elapsed())


STAGES: dict[S, Callable[[AsyncSession, PipelineJob, AgentDependencies], Awaitable[None]]] = {
    S.PROCESSING: stage_processing,
    S.EXTRACTING: stage_extracting,
    S.VERIFYING: stage_verifying,
    S.WRITING: stage_writing,
    S.REVIEWING: stage_reviewing,
    S.PUBLISHING: stage_publishing,
}


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------

async def advance_job(db: AsyncSession, job: PipelineJob, deps: AgentDependencies, *, max_stages: int = 10) -> S:
    """Run stages until the job parks (human), terminates, or fails.
    Commits after every stage (checkpoint)."""
    for _ in range(max_stages):
        status = S(job.status)
        if status == S.QUEUED:
            resume_at = (job.payload or {}).get("resume_at")
            if resume_at:
                job.payload = {k: v for k, v in (job.payload or {}).items() if k != "resume_at"}
            target = S(resume_at) if resume_at else S.PROCESSING
            await jobs.transition(db, job, target, stage="started")
            await db.commit()
            continue
        stage = STAGES.get(status)
        if stage is None:
            return status  # parked or terminal
        await stage(db, job, deps)
        await jobs.renew_lease(db, job)
        await db.commit()
    return S(job.status)


async def run_job(session_factory: async_sessionmaker, job_id: uuid.UUID, deps: AgentDependencies) -> S:
    async with session_factory() as db:
        job = await db.get(PipelineJob, job_id)
        if job is None:
            raise LookupError(f"job {job_id} not found")
        try:
            status = await advance_job(db, job, deps)
            await jobs.release(db, job)
            await db.commit()
            return status
        except Exception as exc:  # noqa: BLE001 - every failure goes through the retry policy
            error: AgentError = classify_exception(exc)
            await db.rollback()  # discard the half-finished stage
            job = await db.get(PipelineJob, job_id)
            log_stage("stage_error", job_id=job.id, correlation_id=job.correlation_id, status=job.status,
                      error_code=error.code, category=error.category.value)
            result = await jobs.handle_failure(db, job, error)
            await db.commit()
            return result


async def run_due_jobs(session_factory: async_sessionmaker, deps: AgentDependencies, *, limit: int | None = None) -> dict[str, int]:
    """Claim and run due jobs. Safe to call from several workers at once."""
    async with session_factory() as db:
        claimed = await jobs.claim_due_jobs(db, limit=limit)
        ids = [job.id for job in claimed]
        await db.commit()
    summary: dict[str, int] = {}
    for job_id in ids:
        status = await run_job(session_factory, job_id, deps)
        summary[status.value] = summary.get(status.value, 0) + 1
    return summary


async def pending_publication_count(db: AsyncSession) -> int:
    return (
        await db.execute(select(func.count()).select_from(Publication).where(
            Publication.status.in_([PublicationStatus.AWAITING_MANUAL.value, PublicationStatus.UNKNOWN.value])))
    ).scalar_one()
