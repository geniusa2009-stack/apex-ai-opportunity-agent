"""APEX AI Opportunity Agent HTTP API.

Ingestion
* POST /agent/whatsapp/webhook   -- signed WhatsApp connector webhook (no user auth;
                                    the connector signature IS the auth)
* GET  /agent/whatsapp/webhook   -- Meta Cloud API verification handshake
* POST /agent/submissions        -- a volunteer (Moderator+) submits an opportunity

Human-in-the-loop administration (Moderator+, source management Owner-only)
* GET  /admin/agent/approvals                 drafts waiting for a human
* POST /admin/agent/drafts/{id}/approve|reject|request-revision
* GET  /admin/agent/jobs[/{id}]               jobs + full event trail
* POST /admin/agent/jobs/{id}/requeue|continue|discard
* POST /admin/agent/publications/{id}/confirm manual publication done
* POST /admin/agent/publications/{id}/performance
* GET  /admin/agent/metrics
* GET/PATCH /admin/agent/sources
* POST /admin/agent/run                       run due jobs now (no Celery needed)
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select

from app.agent import jobs
from app.agent.approval import (
    ApprovalError,
    approve_draft,
    continue_job,
    discard_job,
    reject_draft,
    request_revision,
)
from app.agent.connectors.whatsapp import (
    MAX_BODY_BYTES,
    CloudApiWhatsAppConnector,
    GateDecision,
    PayloadError,
    SignatureError,
    build_gate,
    build_whatsapp_connector,
    to_ingestion_event,
)
from app.agent.ingestion import IngestionEvent, ingest_event
from app.agent.metrics import compute_metrics
from app.agent.observability import log_stage
from app.agent.performance import record_performance
from app.agent.publishing import confirm_manual_publication
from app.api.deps import (
    CurrentUser,
    DbSession,
    RequireModerationAccess,
    RequireOwner,
    RequireSeniorModerationAccess,
    effective_admin_role,
)
from app.core.config import settings
from app.models.agent import (
    AgentPostDraft,
    PipelineJob,
    PipelineJobEvent,
    Publication,
    SourceMessage,
)
from app.models.agent_enums import DraftStatus, PipelineJobStatus, PublicationStatus
from app.models.enums import SourceType
from app.models.opportunity import Opportunity
from app.models.source import Source
from app.schemas.agent import (
    ApprovalItemOut,
    ApproveRequest,
    ConfirmPublicationRequest,
    DecisionResponse,
    DiscardRequest,
    DraftOut,
    IngestResponse,
    JobDecisionRequest,
    JobDetailOut,
    JobEventOut,
    JobOut,
    PerformanceOut,
    PerformanceRequest,
    PublicationOut,
    RejectRequest,
    RevisionRequest,
    SourceOut,
    SourceUpdateRequest,
    VolunteerSubmission,
    WebhookResponse,
)
from app.workers.agent_tasks import trigger_pipeline

router = APIRouter(tags=["ai-opportunity-agent"])


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(getattr(exc, "message", exc)))


# --------------------------------------------------------------------------
# WhatsApp webhook
# --------------------------------------------------------------------------

@router.get("/agent/whatsapp/webhook", response_class=PlainTextResponse)
async def whatsapp_verify(request: Request) -> PlainTextResponse:
    connector = build_whatsapp_connector(settings)
    if not isinstance(connector, CloudApiWhatsAppConnector):
        raise HTTPException(status_code=404, detail="cloud api connector not enabled")
    challenge = connector.verify_subscription(dict(request.query_params))
    if challenge is None:
        raise HTTPException(status_code=403, detail="verification failed")
    return PlainTextResponse(challenge)


@router.post("/agent/whatsapp/webhook", response_model=WebhookResponse)
async def whatsapp_webhook(request: Request, db: DbSession) -> WebhookResponse:
    connector = build_whatsapp_connector(settings)
    if connector is None:
        raise HTTPException(status_code=404, detail="whatsapp connector disabled")
    declared = request.headers.get("content-length", "0")
    if not declared.isdigit() or int(declared) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    body = await request.body()
    try:
        messages = connector.parse_webhook(request.headers, body)
    except SignatureError:
        log_stage("whatsapp_signature_rejected", connector=connector.name)
        raise HTTPException(status_code=401, detail="invalid signature") from None
    except PayloadError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc

    gate = build_gate(settings)
    by_status: dict[str, int] = {}
    accepted = 0
    for message in messages:
        decision = gate.evaluate(message)
        if decision != GateDecision.ACCEPTED:
            # Refused messages are NOT stored (privacy); only counted.
            by_status[decision.value] = by_status.get(decision.value, 0) + 1
            continue
        # The allowlist proved access -> a new Source for this group/sender is approved.
        result = await ingest_event(db, to_ingestion_event(message), auto_approve_source=True)
        by_status[result.status.value] = by_status.get(result.status.value, 0) + 1
        accepted += int(result.accepted)
    await db.commit()
    log_stage("whatsapp_webhook", connector=connector.name, received=len(messages), accepted=accepted)
    if accepted:
        trigger_pipeline()
    return WebhookResponse(received=len(messages), accepted=accepted, by_status=by_status)


# --------------------------------------------------------------------------
# Volunteer submissions
# --------------------------------------------------------------------------

@router.post("/agent/submissions", response_model=IngestResponse, status_code=status.HTTP_202_ACCEPTED)
async def submit_opportunity(
    payload: VolunteerSubmission, current_user: CurrentUser, db: DbSession, _: RequireModerationAccess
) -> IngestResponse:
    text = payload.text if not payload.note else f"{payload.text}\n\n[ملاحظة المتطوع: {payload.note}]"
    event = IngestionEvent(
        source_type=SourceType.VOLUNTEER,
        source_key=str(current_user.id),
        source_display_name=current_user.full_name or current_user.email,
        external_message_id=None,
        sender_id=str(current_user.id),
        sender_name=current_user.full_name,
        text=text,
        links=payload.links,
    )
    # An authenticated team member is an approved source by definition.
    result = await ingest_event(db, event, auto_approve_source=True)
    await db.commit()
    if result.accepted:
        trigger_pipeline()
    return IngestResponse(
        status=result.status.value,
        source_message_id=result.source_message.id if result.source_message else None,
        job_id=result.job.id if result.job else None,
        reason=result.reason,
    )


# --------------------------------------------------------------------------
# Approvals
# --------------------------------------------------------------------------

@router.get("/admin/agent/approvals", response_model=list[ApprovalItemOut])
async def list_approvals(db: DbSession, _: RequireModerationAccess, limit: int = Query(50, ge=1, le=200)) -> list[ApprovalItemOut]:
    drafts = (
        await db.execute(
            select(AgentPostDraft)
            .where(AgentPostDraft.status == DraftStatus.WAITING_APPROVAL.value)
            .order_by(AgentPostDraft.created_at.asc())
            .limit(limit)
        )
    ).scalars().all()
    items: list[ApprovalItemOut] = []
    for draft in drafts:
        job = await db.get(PipelineJob, draft.job_id) if draft.job_id else None
        opportunity = await db.get(Opportunity, draft.opportunity_id)
        payload = (job.payload or {}) if job else {}
        trust = None
        if job and job.source_message_id:
            message = await db.get(SourceMessage, job.source_message_id)
            source = await db.get(Source, message.source_id) if message else None
            trust = source.trust_score if source else None
        items.append(ApprovalItemOut(
            draft=DraftOut.model_validate(draft),
            opportunity_title=opportunity.title if opportunity else "",
            verification=payload.get("verification"),
            dedup_outcome=payload.get("dedup_outcome"),
            duplicate_of=payload.get("duplicate_of"),
            update_proposal=payload.get("update_proposal"),
            source_trust_score=trust,
        ))
    return items


async def _draft_or_404(db, draft_id: uuid.UUID) -> AgentPostDraft:
    draft = await db.get(AgentPostDraft, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="draft not found")
    return draft


@router.post("/admin/agent/drafts/{draft_id}/approve", response_model=DecisionResponse)
async def approve(draft_id: uuid.UUID, payload: ApproveRequest, current_user: CurrentUser, db: DbSession,
                  _: RequireModerationAccess) -> DecisionResponse:
    draft = await _draft_or_404(db, draft_id)
    try:
        result = await approve_draft(db, draft, actor_id=current_user.id, actor_role=effective_admin_role(current_user),
                                     notes=payload.notes, edited_body=payload.edited_body)
    except ApprovalError as exc:
        raise _bad_request(exc) from exc
    await db.commit()
    trigger_pipeline()
    job = await db.get(PipelineJob, draft.job_id)
    return DecisionResponse(success=True, job_status=job.status if job else None, draft_status=draft.status,
                            warnings=result["warnings"], updated_fields=result["updated_fields"])


@router.post("/admin/agent/drafts/{draft_id}/reject", response_model=DecisionResponse)
async def reject(draft_id: uuid.UUID, payload: RejectRequest, current_user: CurrentUser, db: DbSession,
                 _: RequireModerationAccess) -> DecisionResponse:
    draft = await _draft_or_404(db, draft_id)
    try:
        await reject_draft(db, draft, actor_id=current_user.id, actor_role=effective_admin_role(current_user),
                           reason=payload.reason, inaccurate_source=payload.inaccurate_source)
    except ApprovalError as exc:
        raise _bad_request(exc) from exc
    await db.commit()
    return DecisionResponse(success=True, draft_status=draft.status, job_status=PipelineJobStatus.COMPLETED.value)


@router.post("/admin/agent/drafts/{draft_id}/request-revision", response_model=DecisionResponse)
async def revise(draft_id: uuid.UUID, payload: RevisionRequest, current_user: CurrentUser, db: DbSession,
                 _: RequireModerationAccess) -> DecisionResponse:
    draft = await _draft_or_404(db, draft_id)
    try:
        await request_revision(db, draft, actor_id=current_user.id, notes=payload.notes)
    except ApprovalError as exc:
        raise _bad_request(exc) from exc
    await db.commit()
    trigger_pipeline()
    return DecisionResponse(success=True, draft_status=draft.status, job_status=PipelineJobStatus.WRITING.value)


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------

@router.get("/admin/agent/jobs", response_model=list[JobOut])
async def list_jobs(
    db: DbSession,
    _: RequireModerationAccess,
    job_status: Annotated[str | None, Query(alias="status")] = None,
    limit: int = Query(50, ge=1, le=200),
) -> list[JobOut]:
    query = select(PipelineJob).order_by(PipelineJob.created_at.desc()).limit(limit)
    if job_status:
        query = query.where(PipelineJob.status == job_status)
    return [JobOut.model_validate(j) for j in (await db.execute(query)).scalars().all()]


async def _job_or_404(db, job_id: uuid.UUID) -> PipelineJob:
    job = await db.get(PipelineJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@router.get("/admin/agent/jobs/{job_id}", response_model=JobDetailOut)
async def job_detail(job_id: uuid.UUID, db: DbSession, _: RequireModerationAccess) -> JobDetailOut:
    job = await _job_or_404(db, job_id)
    events = (await db.execute(
        select(PipelineJobEvent).where(PipelineJobEvent.job_id == job.id).order_by(PipelineJobEvent.created_at.asc())
    )).scalars().all()
    message = await db.get(SourceMessage, job.source_message_id) if job.source_message_id else None
    base = JobOut.model_validate(job).model_dump()
    return JobDetailOut(
        **base,
        payload=job.payload,
        events=[JobEventOut.model_validate(e) for e in events],
        message_excerpt=message.raw_text[:1000] if message else None,
        message_links=message.links if message else None,
    )


@router.post("/admin/agent/jobs/{job_id}/requeue", response_model=DecisionResponse)
async def requeue_job(job_id: uuid.UUID, db: DbSession, _: RequireModerationAccess) -> DecisionResponse:
    job = await _job_or_404(db, job_id)
    try:
        await jobs.requeue(db, job)
    except jobs.InvalidTransitionError as exc:
        raise _bad_request(exc) from exc
    await db.commit()
    trigger_pipeline()
    return DecisionResponse(success=True, job_status=job.status)


@router.post("/admin/agent/jobs/{job_id}/continue", response_model=DecisionResponse)
async def continue_after_review(job_id: uuid.UUID, payload: JobDecisionRequest, current_user: CurrentUser,
                                db: DbSession, _: RequireModerationAccess) -> DecisionResponse:
    job = await _job_or_404(db, job_id)
    try:
        await continue_job(db, job, actor_id=current_user.id, notes=payload.notes)
    except (ApprovalError, jobs.InvalidTransitionError) as exc:
        raise _bad_request(exc) from exc
    await db.commit()
    trigger_pipeline()
    return DecisionResponse(success=True, job_status=job.status)


@router.post("/admin/agent/jobs/{job_id}/discard", response_model=DecisionResponse)
async def discard(job_id: uuid.UUID, payload: DiscardRequest, current_user: CurrentUser, db: DbSession,
                  _: RequireModerationAccess) -> DecisionResponse:
    job = await _job_or_404(db, job_id)
    try:
        await discard_job(db, job, actor_id=current_user.id, reason=payload.reason)
    except ApprovalError as exc:
        raise _bad_request(exc) from exc
    await db.commit()
    return DecisionResponse(success=True, job_status=job.status)


@router.post("/admin/agent/run")
async def run_now(_: RequireSeniorModerationAccess, limit: int = Query(10, ge=1, le=50)) -> dict:
    """Run due jobs inside this request (useful without a Celery worker)."""
    from app.agent.pipeline import build_dependencies, run_due_jobs
    from app.db.session import AsyncSessionLocal

    return await run_due_jobs(AsyncSessionLocal, build_dependencies(), limit=limit)


# --------------------------------------------------------------------------
# Publications / performance
# --------------------------------------------------------------------------

@router.get("/admin/agent/publications", response_model=list[PublicationOut])
async def list_publications(db: DbSession, _: RequireModerationAccess,
                            pub_status: Annotated[str | None, Query(alias="status")] = None,
                            limit: int = Query(50, ge=1, le=200)) -> list[PublicationOut]:
    query = select(Publication).order_by(Publication.created_at.desc()).limit(limit)
    if pub_status:
        query = query.where(Publication.status == pub_status)
    return [PublicationOut.model_validate(p) for p in (await db.execute(query)).scalars().all()]


@router.post("/admin/agent/publications/{publication_id}/confirm", response_model=PublicationOut)
async def confirm_publication(publication_id: uuid.UUID, payload: ConfirmPublicationRequest, db: DbSession,
                              _: RequireModerationAccess) -> PublicationOut:
    publication = await db.get(Publication, publication_id)
    if publication is None:
        raise HTTPException(status_code=404, detail="publication not found")
    try:
        await confirm_manual_publication(db, publication, external_url=payload.external_url,
                                         external_id=payload.external_id)
    except Exception as exc:  # noqa: BLE001
        raise _bad_request(exc) from exc
    others = (await db.execute(select(Publication).where(Publication.draft_id == publication.draft_id))).scalars().all()
    if all(p.status == PublicationStatus.PUBLISHED.value for p in others):
        draft = await db.get(AgentPostDraft, publication.draft_id)
        if draft is not None:
            draft.status = DraftStatus.PUBLISHED.value
    await db.commit()
    return PublicationOut.model_validate(publication)


@router.post("/admin/agent/publications/{publication_id}/performance", response_model=PerformanceOut,
             status_code=status.HTTP_201_CREATED)
async def add_performance(publication_id: uuid.UUID, payload: PerformanceRequest, db: DbSession,
                          _: RequireModerationAccess) -> PerformanceOut:
    publication = await db.get(Publication, publication_id)
    if publication is None:
        raise HTTPException(status_code=404, detail="publication not found")
    if publication.status != PublicationStatus.PUBLISHED.value:
        raise HTTPException(status_code=409, detail="only a published post has performance")
    row = await record_performance(db, publication, payload)
    await db.commit()
    return PerformanceOut.model_validate(row)


@router.get("/admin/agent/metrics")
async def metrics(db: DbSession, _: RequireModerationAccess, days: int = Query(30, ge=1, le=365)) -> dict:
    return await compute_metrics(db, since=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days))


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------

@router.get("/admin/agent/sources", response_model=list[SourceOut])
async def list_sources(db: DbSession, _: RequireModerationAccess, access_status: str | None = None,
                       limit: int = Query(100, ge=1, le=500)) -> list[SourceOut]:
    query = select(Source).order_by(Source.created_at.desc()).limit(limit)
    if access_status:
        query = query.where(Source.access_status == access_status)
    return [SourceOut.model_validate(s) for s in (await db.execute(query)).scalars().all()]


@router.patch("/admin/agent/sources/{source_id}", response_model=SourceOut)
async def update_source(source_id: uuid.UUID, payload: SourceUpdateRequest, db: DbSession, _: RequireOwner) -> SourceOut:
    source = await db.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="source not found")
    for field in ("trust_score", "access_status", "active_status"):
        value = getattr(payload, field)
        if value is not None:
            setattr(source, field, value)
    await db.commit()
    return SourceOut.model_validate(source)
