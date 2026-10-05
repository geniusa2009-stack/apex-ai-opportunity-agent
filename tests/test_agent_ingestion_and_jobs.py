"""Phase 2 -- SourceMessage, source trust, unified ingestion and the
persistent PipelineJob runner primitives (retry, backoff, dead letter,
restart recovery). Real ORM models on in-memory SQLite."""

from __future__ import annotations

import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.agent import jobs
from app.agent.errors import (
    AuthenticationError,
    DataError,
    RateLimitError,
    TransientError,
    ValidationFailedError,
)
from app.agent.ingestion import (
    AttachmentMeta,
    IngestionEvent,
    IngestStatus,
    ingest_event,
    source_name_for,
)
from app.agent.reputation import (
    MAX_TRUST,
    MIN_TRUST,
    TrustEvent,
    default_trust_for,
    job_priority,
    requires_official_confirmation,
    updated_trust,
)
from app.models.agent import PipelineJob, PipelineJobEvent, SourceMessage
from app.models.agent_enums import PipelineJobStatus as S
from app.models.agent_enums import SourceMessageStatus
from app.models.enums import SourceType
from tests.agent_support import (
    SAMPLE_OPPORTUNITY_TEXT,
    make_engine_and_session_factory,
    make_source,
)


@pytest_asyncio.fixture
async def session():
    engine, factory = await make_engine_and_session_factory()
    async with factory() as db:
        yield db
    await engine.dispose()


def _event(**overrides) -> IngestionEvent:
    data = dict(
        source_type=SourceType.WHATSAPP,
        source_key="group-1@g.us",
        source_display_name="Scholarships group",
        external_message_id="wamid.1",
        sender_id="201000000001",
        sender_name="Volunteer A",
        group_id="group-1@g.us",
        group_name="Scholarships group",
        text=SAMPLE_OPPORTUNITY_TEXT,
    )
    data.update(overrides)
    return IngestionEvent(**data)


async def _count(db, model) -> int:
    return (await db.execute(select(func.count()).select_from(model))).scalar_one()


# --------------------------------------------------------------------------
# Ingestion
# --------------------------------------------------------------------------

class TestIngestion:
    pytestmark = pytest.mark.asyncio

    async def test_new_message_is_stored_normalized_and_queued_with_trust_priority(self, session):
        result = await ingest_event(session, _event(), auto_approve_source=True)
        assert result.status == IngestStatus.QUEUED
        message = result.source_message
        assert message.processing_status == SourceMessageStatus.QUEUED.value
        assert message.content_hash and len(message.content_hash) == 64
        assert any("daad.de" in link for link in message.links)
        assert message.group_id == "group-1@g.us"
        assert result.source.name == source_name_for(SourceType.WHATSAPP, "group-1@g.us")
        assert result.source.trust_score == default_trust_for("whatsapp")
        assert result.job.status == S.QUEUED.value
        assert result.job.priority == job_priority(result.source.trust_score)
        assert result.job.source_message_id == message.id
        events = (await session.execute(select(PipelineJobEvent))).scalars().all()
        assert [e.to_status for e in events] == ["queued"]

    async def test_same_platform_message_twice_is_stored_once(self, session):
        first = await ingest_event(session, _event(), auto_approve_source=True)
        second = await ingest_event(session, _event(), auto_approve_source=True)
        assert second.status == IngestStatus.DUPLICATE_DELIVERY
        assert second.source_message.id == first.source_message.id
        assert await _count(session, SourceMessage) == 1
        assert await _count(session, PipelineJob) == 1

    async def test_forwarded_copy_with_decorations_is_an_exact_content_duplicate(self, session):
        await ingest_event(session, _event(), auto_approve_source=True)
        forwarded = "*Forwarded*\n" + SAMPLE_OPPORTUNITY_TEXT.replace("utm_source=whatsapp&utm_medium=group", "fbclid=XYZ")
        result = await ingest_event(
            session,
            _event(source_key="group-2@g.us", group_id="group-2@g.us", external_message_id="wamid.2", text=forwarded),
            auto_approve_source=True,
        )
        assert result.status == IngestStatus.DUPLICATE_CONTENT
        assert result.source_message.processing_status == SourceMessageStatus.DUPLICATE_MESSAGE.value
        assert await _count(session, PipelineJob) == 1  # no second job / no second AI call

    async def test_pending_or_revoked_source_is_refused(self, session):
        pending = await ingest_event(session, _event(source_key="unknown@g.us"), auto_approve_source=False)
        assert pending.status == IngestStatus.REJECTED_SOURCE
        assert pending.source.access_status == "pending"
        source = await make_source(session, name="whatsapp:revoked@g.us", access_status="revoked")
        revoked = await ingest_event(session, _event(source_key="revoked@g.us"), auto_approve_source=True)
        assert revoked.status == IngestStatus.REJECTED_SOURCE and revoked.source.id == source.id
        assert await _count(session, SourceMessage) == 0

    async def test_bot_message_is_ignored_and_never_stored(self, session):
        result = await ingest_event(session, _event(from_bot=True), auto_approve_source=True)
        assert result.status == IngestStatus.IGNORED_SELF
        assert await _count(session, SourceMessage) == 0

    async def test_malformed_event_is_rejected(self, session):
        result = await ingest_event(session, _event(text="   ", links=[], attachments=[]), auto_approve_source=True)
        assert result.status == IngestStatus.REJECTED_MALFORMED

    async def test_attachment_only_message_with_caption_is_accepted(self, session):
        result = await ingest_event(
            session,
            _event(text="", attachments=[AttachmentMeta(kind="image", caption="منحة جديدة https://example.org/apply")]),
            auto_approve_source=True,
        )
        assert result.status == IngestStatus.QUEUED
        assert "https://example.org/apply" in result.source_message.links

    async def test_overlong_text_is_capped(self, session):
        event = _event(text="x" * 50_000)
        assert len(event.text) == 20_000


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------

async def _queued_job(db, **kwargs) -> PipelineJob:
    job = await jobs.create_job(db, **kwargs)
    await db.flush()
    return job


def test_backoff_is_exponential_and_capped():
    assert [jobs.backoff_seconds(n, base=30, cap=3600) for n in range(1, 9)] == [
        30, 60, 120, 240, 480, 960, 1920, 3600,
    ]
    assert jobs.backoff_seconds(1000, base=30, cap=3600) == 3600


class TestJobs:
    pytestmark = pytest.mark.asyncio

    async def test_claim_leases_and_orders_by_priority(self, session):
        low = await _queued_job(session, priority=10)
        high = await _queued_job(session, priority=90)
        claimed = await jobs.claim_due_jobs(session, limit=1, owner="w1")
        assert [j.id for j in claimed] == [high.id]
        assert claimed[0].locked_by == "w1" and claimed[0].locked_until is not None
        # The leased job is not claimable by another worker while the lease is live.
        claimed2 = await jobs.claim_due_jobs(session, limit=5, owner="w2")
        assert [j.id for j in claimed2] == [low.id]

    async def test_invalid_transition_is_refused(self, session):
        job = await _queued_job(session)
        with pytest.raises(jobs.InvalidTransitionError):
            await jobs.transition(session, job, S.WAITING_APPROVAL)

    async def test_transient_failure_schedules_retry_at_same_stage(self, session):
        job = await _queued_job(session)
        await jobs.transition(session, job, S.PROCESSING)
        await jobs.transition(session, job, S.EXTRACTING)
        now = dt.datetime(2026, 10, 5, 12, 0, tzinfo=dt.timezone.utc)
        status = await jobs.handle_failure(session, job, TransientError("LLM timeout", code="LLM_TIMEOUT"), now=now)
        assert status == S.EXTRACTING
        assert job.attempts == 1
        assert jobs._aware(job.next_retry_at) == now + dt.timedelta(seconds=jobs.backoff_seconds(1))
        assert job.last_error_code == "LLM_TIMEOUT" and job.last_error_category == "transient"
        # Not runnable before next_retry_at, runnable after.
        assert await jobs.claim_due_jobs(session, now=now + dt.timedelta(seconds=1)) == []
        later = await jobs.claim_due_jobs(session, now=now + dt.timedelta(hours=2))
        assert [j.id for j in later] == [job.id]

    async def test_dead_letter_after_max_attempts_no_infinite_retry(self, session):
        job = await _queued_job(session, max_attempts=3)
        await jobs.transition(session, job, S.PROCESSING)
        statuses = [await jobs.handle_failure(session, job, RateLimitError("429", code="RATE_LIMITED")) for _ in range(3)]
        assert statuses == [S.PROCESSING, S.PROCESSING, S.DEAD_LETTER]
        assert job.attempts == 3 and job.status == S.DEAD_LETTER.value
        assert job.completed_at is not None
        assert await jobs.claim_due_jobs(session, now=jobs.utcnow() + dt.timedelta(days=30)) == []

    async def test_validation_and_data_errors_go_to_needs_review_without_retry(self, session):
        for error in (ValidationFailedError("bad schema"), DataError("no content")):
            job = await _queued_job(session)
            await jobs.transition(session, job, S.EXTRACTING)
            assert await jobs.handle_failure(session, job, error) == S.NEEDS_REVIEW
            assert job.attempts == 0

    async def test_auth_errors_fail_without_retry(self, session):
        job = await _queued_job(session)
        await jobs.transition(session, job, S.EXTRACTING)
        assert await jobs.handle_failure(session, job, AuthenticationError("401")) == S.FAILED

    async def test_restart_recovery_reclaims_expired_lease_only(self, session):
        job = await _queued_job(session)
        t0 = dt.datetime(2026, 10, 5, 12, 0, tzinfo=dt.timezone.utc)
        [claimed] = await jobs.claim_due_jobs(session, owner="crashed-worker", now=t0, lease_seconds=300)
        await jobs.transition(session, claimed, S.PROCESSING)
        await jobs.transition(session, claimed, S.EXTRACTING)
        # Worker "crashes" here: lease still live -> nobody else may take it.
        assert await jobs.claim_due_jobs(session, owner="w2", now=t0 + dt.timedelta(seconds=60)) == []
        # Lease expired -> recovered by another worker at the SAME stage.
        [recovered] = await jobs.claim_due_jobs(session, owner="w2", now=t0 + dt.timedelta(seconds=301))
        assert recovered.id == job.id and recovered.status == S.EXTRACTING.value and recovered.locked_by == "w2"
        stages = [e.stage for e in (await session.execute(select(PipelineJobEvent))).scalars().all()]
        assert "lease_recovered" in stages

    async def test_manual_requeue_resumes_at_last_stage(self, session):
        job = await _queued_job(session, max_attempts=1)
        await jobs.transition(session, job, S.PROCESSING)
        await jobs.transition(session, job, S.EXTRACTING)
        await jobs.handle_failure(session, job, TransientError("x"))
        assert job.status == S.DEAD_LETTER.value
        await jobs.requeue(session, job)
        assert job.status == S.QUEUED.value and job.attempts == 0
        assert job.payload["resume_at"] == "extracting"

    async def test_requeue_refuses_active_jobs(self, session):
        job = await _queued_job(session)
        with pytest.raises(jobs.InvalidTransitionError):
            await jobs.requeue(session, job)

    async def test_every_transition_is_audited(self, session):
        job = await _queued_job(session)
        await jobs.transition(session, job, S.PROCESSING, stage="normalize")
        await jobs.transition(session, job, S.COMPLETED, outcome="not_opportunity")
        events = (await session.execute(select(PipelineJobEvent).order_by(PipelineJobEvent.created_at))).scalars().all()
        assert [(e.from_status, e.to_status) for e in events] == [
            (None, "queued"), ("queued", "processing"), ("processing", "completed"),
        ]
        assert job.outcome == "not_opportunity"


# --------------------------------------------------------------------------
# Reputation
# --------------------------------------------------------------------------

class TestReputation:
    def test_updates_are_bounded_and_asymmetric(self):
        value = 0.5
        for _ in range(100):
            value = updated_trust(value, TrustEvent.HUMAN_APPROVED)
        assert value == MAX_TRUST  # never "perfect"
        for _ in range(100):
            value = updated_trust(value, TrustEvent.HUMAN_REJECTED_INACCURATE)
        assert value == MIN_TRUST  # never zero
        assert abs(updated_trust(0.5, TrustEvent.HUMAN_REJECTED_INACCURATE) - 0.5) > abs(
            updated_trust(0.5, TrustEvent.HUMAN_APPROVED) - 0.5
        )

    def test_trust_sets_priority_and_evidence_requirements_only(self):
        assert job_priority(0.9) > job_priority(0.4)
        assert requires_official_confirmation(0.4) is True
        assert requires_official_confirmation(0.9) is False
        assert default_trust_for("official") > default_trust_for("whatsapp")
