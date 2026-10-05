"""Phases 8, 9, 12, 13 -- approval/state machine, idempotent publishing,
WhatsApp connectors and the full end-to-end pipeline:

    WhatsApp message -> SourceMessage -> normalization -> dedup -> extraction
    -> verification -> APEX writer -> reviewer -> human approval -> publisher
    -> performance record

Everything external is faked: the LLM (FakeLLMProvider), the official page
(FakeFetcher), WhatsApp (mock / signed bridge / Cloud API payloads) and the
publishers (MockPublisher, Telegram via httpx.MockTransport). The HTTP layer
is the real FastAPI app with only `get_db`/`get_current_user` overridden.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import time
import uuid

import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.agent import jobs
from app.agent.connectors.whatsapp import (
    BridgeWhatsAppConnector,
    CloudApiWhatsAppConnector,
    GateDecision,
    SignatureError,
    WhatsAppGate,
    WhatsAppInboundMessage,
    sign_bridge_payload,
)
from app.agent.errors import RateLimitError
from app.agent.ingestion import IngestionEvent, ingest_event
from app.agent.llm import LLMClient
from app.agent.llm.fake import FakeLLMProvider
from app.agent.pipeline import AgentDependencies, run_due_jobs, run_job
from app.agent.publishing import (
    MockPublisher,
    PublishPayload,
    TelegramBotPublisher,
    publish_draft,
)
from app.agent.verification import FetchedPage, html_to_text
from app.api.deps import get_current_user
from app.core.config import settings
from app.db.session import get_db
from app.main import app
from app.models.agent import (
    AgentPostDraft,
    OpportunitySourceMessage,
    PipelineJob,
    Publication,
    SourceMessage,
)
from app.models.agent_enums import DraftStatus, JobOutcome, PublicationStatus
from app.models.agent_enums import PipelineJobStatus as S
from app.models.enums import AdminRole, OpportunityReviewStatus, SourceType
from app.models.moderation_log import ModerationLogEntry
from app.models.opportunity import Opportunity
from app.models.opportunity_change_history import OpportunityChangeHistory
from app.models.source import Source
from app.models.user import User
from tests.agent_support import (
    SAMPLE_OPPORTUNITY_TEXT,
    make_engine_and_session_factory,
    sample_extraction,
)

TODAY = dt.date(2026, 10, 5)
APP_URL = "https://www.daad.de/en/study-and-research-in-germany/scholarships/"
OFFICIAL_HTML = """<html><head><title>DAAD Scholarships Germany</title></head><body>
<h1>DAAD Master's Scholarship in Germany 2027</h1><p>German Academic Exchange Service (DAAD)</p>
<p>Application deadline: 15 October 2026</p><p>Extended deadline: 30 November 2026</p></body></html>"""
GROUP = "120363000000000001@g.us"


class FakeFetcher:
    def __init__(self, html: str = OFFICIAL_HTML, status: int = 200):
        self.html, self.status, self.calls = html, status, 0

    async def fetch(self, url: str) -> FetchedPage:
        self.calls += 1
        title, text = html_to_text(self.html)
        return FetchedPage(url=url, final_url=url, status_code=self.status, title=title, text=text,
                           error=None if self.status < 400 else f"HTTP {self.status}")


def make_deps(llm_replies=None, *, publishers=None, fetcher=None, writer_mode="template") -> AgentDependencies:
    provider = FakeLLMProvider(llm_replies or [sample_extraction()])
    return AgentDependencies(
        llm=LLMClient(primary=provider, max_validation_retries=1),
        fetcher=fetcher or FakeFetcher(),
        publishers=publishers if publishers is not None else [MockPublisher()],
        writer_mode=writer_mode,
        today=lambda: TODAY,
    )


@pytest_asyncio.fixture
async def factory(monkeypatch):
    monkeypatch.setattr(settings, "agent_celery_trigger_enabled", False)
    engine, session_factory = await make_engine_and_session_factory()
    yield session_factory
    await engine.dispose()


def moderator() -> User:
    return User(id=uuid.uuid4(), email="mod@apex.test", full_name="Mod", is_active=True, is_superuser=False,
                admin_role=AdminRole.MODERATOR)


@pytest.fixture
def client(factory, monkeypatch):
    async def _db():
        async with factory() as session:
            yield session

    user = moderator()
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: user
    monkeypatch.setattr(settings, "whatsapp_connector", "mock")
    monkeypatch.setattr(settings, "allowed_whatsapp_group_ids", GROUP)
    monkeypatch.setattr(settings, "whatsapp_bot_ids", "201000000999")
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def wa_message(message_id="wamid.1", **overrides) -> dict:
    data = {"message_id": message_id, "sender_id": "201000000001@s.whatsapp.net", "sender_name": "Volunteer",
            "group_id": GROUP, "group_name": "APEX Scholarships", "text": SAMPLE_OPPORTUNITY_TEXT,
            "timestamp": "2026-10-05T09:00:00+00:00"}
    data.update(overrides)
    return data


async def _get(factory, model, **filters):
    async with factory() as db:
        query = select(model)
        for key, value in filters.items():
            query = query.where(getattr(model, key) == value)
        return (await db.execute(query)).scalars().all()


async def _count(factory, model) -> int:
    async with factory() as db:
        return (await db.execute(select(func.count()).select_from(model))).scalar_one()


# ==========================================================================
# THE end-to-end scenario
# ==========================================================================

@pytest.mark.asyncio
async def test_end_to_end_whatsapp_to_published_post_with_performance(client, factory):
    # 1. WhatsApp message arrives through the (mock) connector webhook.
    response = client.post("/api/v1/agent/whatsapp/webhook", json={"messages": [wa_message()]})
    assert response.status_code == 200, response.text
    assert response.json()["accepted"] == 1

    [message] = await _get(factory, SourceMessage)
    assert message.group_id == GROUP and message.content_hash

    # 2. Worker runs: normalization -> dedup -> extraction -> verification -> writer -> reviewer.
    deps = make_deps()
    summary = await run_due_jobs(factory, deps)
    assert summary == {"waiting_approval": 1}
    assert deps.llm.primary.calls == 1  # exactly one AI call for the whole message

    [job] = await _get(factory, PipelineJob)
    assert job.status == S.WAITING_APPROVAL.value and job.outcome == JobOutcome.DRAFT_READY.value
    [opportunity] = await _get(factory, Opportunity)
    assert opportunity.review_status == OpportunityReviewStatus.PENDING_REVIEW  # invisible until a human approves
    assert opportunity.deadline.date() == dt.date(2026, 10, 15)
    assert (job.payload["verification"]["level"]) == "official_confirmed"

    # 3. Human sees the draft in the approval queue.
    queue = client.get("/api/v1/admin/agent/approvals").json()
    assert len(queue) == 1
    draft_id = queue[0]["draft"]["id"]
    assert "15 أكتوبر 2026" in queue[0]["draft"]["body"]
    assert queue[0]["verification"]["level"] == "official_confirmed"

    # Nothing is published before approval.
    assert await _count(factory, Publication) == 0

    # 4. Human approves.
    approved = client.post(f"/api/v1/admin/agent/drafts/{draft_id}/approve", json={"notes": "looks right"})
    assert approved.status_code == 200, approved.text
    assert approved.json()["job_status"] == "publishing"
    # A double click is refused, not double-published.
    assert client.post(f"/api/v1/admin/agent/drafts/{draft_id}/approve", json={}).status_code == 409

    # 5. Worker publishes.
    publisher = deps.publishers[0]
    assert (await run_due_jobs(factory, deps)) == {"completed": 1}
    assert len(publisher.sent) == 1
    [publication] = await _get(factory, Publication)
    assert publication.status == PublicationStatus.PUBLISHED.value and publication.external_url
    [draft] = await _get(factory, AgentPostDraft)
    assert draft.status == DraftStatus.PUBLISHED.value
    [opportunity] = await _get(factory, Opportunity)
    assert opportunity.review_status == OpportunityReviewStatus.PUBLISHED
    logs = await _get(factory, ModerationLogEntry)
    assert [log.action.value for log in logs] == ["opportunity_approve"]

    # Running again never re-publishes.
    await run_due_jobs(factory, deps)
    assert len(publisher.sent) == 1

    # 6. Performance is recorded and visible in metrics.
    perf = client.post(f"/api/v1/admin/agent/publications/{publication.id}/performance",
                       json={"views": 500, "reactions": 40, "clicks": 25})
    assert perf.status_code == 201 and perf.json()["engagement_score"] == pytest.approx(0.18)
    metrics = client.get("/api/v1/admin/agent/metrics").json()
    assert metrics["opportunities_received"] == 1
    assert metrics["publications_successful"] == 1
    assert metrics["opportunities_verified"] == 1
    assert metrics["average_processing_time_seconds"] is not None

    # 7. The job's audit trail tells the whole story.
    detail = client.get(f"/api/v1/admin/agent/jobs/{job.id}").json()
    stages = [e["stage"] for e in detail["events"]]
    for stage in ("created", "started", "normalized", "extracted", "verified", "written", "ready_for_human",
                  "human_approved", "published"):
        assert stage in stages


# ==========================================================================
# WhatsApp ingestion rules over HTTP
# ==========================================================================

@pytest.mark.asyncio
async def test_webhook_refuses_unauthorized_group_bot_and_malformed_and_dedups_deliveries(client, factory):
    payload = {"messages": [
        wa_message("m1"),
        wa_message("m1"),  # same delivery twice
        wa_message("m2", group_id="999@g.us"),  # not on the allowlist
        wa_message("m3", sender_id="+20 100 000 0999"),  # the bot itself
        wa_message("m4", from_me=True),  # the bot itself
        wa_message("m5", text="", links=[]),  # malformed
        wa_message("m6", group_id=None),  # direct message from a sender not on the sender allowlist
    ]}
    body = client.post("/api/v1/agent/whatsapp/webhook", json=payload).json()
    assert body["accepted"] == 1
    assert body["by_status"] == {"queued": 1, "duplicate_delivery": 1, "unauthorized_group": 1, "ignored_self": 2,
                                 "malformed": 1, "unauthorized_sender": 1}
    assert await _count(factory, SourceMessage) == 1  # refused messages are not stored


def test_webhook_disabled_and_bad_payload(client, monkeypatch):
    assert client.post("/api/v1/agent/whatsapp/webhook", content=b"{not json").status_code == 400
    monkeypatch.setattr(settings, "whatsapp_connector", "disabled")
    assert client.post("/api/v1/agent/whatsapp/webhook", json={"messages": []}).status_code == 404


def test_signed_bridge_webhook(client, monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_connector", "bridge")
    monkeypatch.setattr(settings, "whatsapp_bridge_hmac_secret", "bridge-secret")
    body = json.dumps({"messages": [wa_message("b1")]}).encode()
    ts = str(int(time.time()))
    good = {"X-Apex-Timestamp": ts, "X-Apex-Signature": sign_bridge_payload("bridge-secret", ts, body),
            "Content-Type": "application/json"}
    assert client.post("/api/v1/agent/whatsapp/webhook", content=body, headers=good).json()["accepted"] == 1
    bad = dict(good, **{"X-Apex-Signature": "sha256=" + "0" * 64})
    assert client.post("/api/v1/agent/whatsapp/webhook", content=body, headers=bad).status_code == 401


def test_cloud_api_verification_handshake(client, monkeypatch):
    monkeypatch.setattr(settings, "whatsapp_connector", "cloud_api")
    monkeypatch.setattr(settings, "whatsapp_cloud_app_secret", "app-secret")
    monkeypatch.setattr(settings, "whatsapp_cloud_verify_token", "verify-me")
    ok = client.get("/api/v1/agent/whatsapp/webhook",
                    params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"})
    assert ok.status_code == 200 and ok.text == "42"
    assert client.get("/api/v1/agent/whatsapp/webhook",
                      params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "42"}).status_code == 403


# ==========================================================================
# Connector units
# ==========================================================================

def test_gate_rules_and_id_normalization():
    gate = WhatsAppGate(frozenset({GROUP}), frozenset({"+20 100 123 4567"}), frozenset({"201000000999"}))

    def msg(**kw):
        return WhatsAppInboundMessage(**{"message_id": "x", "text": "hello opportunity", **kw})

    assert gate.evaluate(msg(group_id=GROUP, sender_id="1")) == GateDecision.ACCEPTED
    assert gate.evaluate(msg(group_id="other@g.us")) == GateDecision.UNAUTHORIZED_GROUP
    assert gate.evaluate(msg(sender_id="201001234567@s.whatsapp.net")) == GateDecision.ACCEPTED
    assert gate.evaluate(msg(sender_id="201009999999")) == GateDecision.UNAUTHORIZED_SENDER
    assert gate.evaluate(msg(sender_id="201000000999@s.whatsapp.net", group_id=GROUP)) == GateDecision.IGNORED_SELF
    assert gate.evaluate(msg(group_id=GROUP, text="")) == GateDecision.MALFORMED


def test_bridge_signature_replay_protection():
    connector = BridgeWhatsAppConnector("s3cret", now=lambda: 1_000_000)
    body = json.dumps({"messages": [wa_message()]}).encode()
    old = "999000"  # far outside the window
    with pytest.raises(SignatureError):
        connector.parse_webhook({"X-Apex-Timestamp": old, "X-Apex-Signature": sign_bridge_payload("s3cret", old, body)}, body)
    fresh = "1000000"
    parsed = connector.parse_webhook({"x-apex-timestamp": fresh, "x-apex-signature": sign_bridge_payload("s3cret", fresh, body)}, body)
    assert parsed[0].group_id == GROUP
    with pytest.raises(SignatureError):  # tampered body
        connector.parse_webhook({"x-apex-timestamp": fresh, "x-apex-signature": sign_bridge_payload("s3cret", fresh, body)}, body + b" ")


def test_cloud_api_payload_parsing_and_signature():
    connector = CloudApiWhatsAppConnector("app-secret", "token")
    payload = {"entry": [{"changes": [{"value": {
        "metadata": {"display_phone_number": "201000000999", "phone_number_id": "1"},
        "contacts": [{"wa_id": "201001234567", "profile": {"name": "Sara"}}],
        "messages": [
            {"from": "201001234567", "id": "wamid.A", "timestamp": "1790000000", "type": "text",
             "text": {"body": "منحة جديدة https://example.org/apply"}},
            {"from": "201001234567", "id": "wamid.B", "timestamp": "1790000001", "type": "image",
             "image": {"mime_type": "image/jpeg", "caption": "بوستر المنحة"}},
            {"from": "201001234567", "id": "wamid.C", "type": "reaction", "reaction": {"emoji": "👍"}},
        ]}}]}]}
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    messages = connector.parse_webhook({"X-Hub-Signature-256": signature}, body)
    assert [m.message_id for m in messages] == ["wamid.A", "wamid.B"]  # reaction ignored
    assert messages[0].sender_name == "Sara" and messages[0].group_id is None
    assert messages[1].attachments[0].caption == "بوستر المنحة"
    with pytest.raises(SignatureError):
        connector.parse_webhook({"X-Hub-Signature-256": "sha256=deadbeef"}, body)


# ==========================================================================
# Pipeline branches
# ==========================================================================

async def _ingest(factory, text=SAMPLE_OPPORTUNITY_TEXT, message_id="m1", source_type=SourceType.WHATSAPP, key=GROUP, trust=None):
    async with factory() as db:
        result = await ingest_event(db, IngestionEvent(source_type=source_type, source_key=key, external_message_id=message_id,
                                                       group_id=key if source_type == SourceType.WHATSAPP else None,
                                                       text=text), auto_approve_source=True)
        if trust is not None:
            result.source.trust_score = trust
        await db.commit()
        return result.job.id if result.job else None


@pytest.mark.asyncio
async def test_chatter_costs_zero_llm_calls(factory):
    job_id = await _ingest(factory, text="شكرا يا جماعة 🙏")
    deps = make_deps()
    assert await run_job(factory, job_id, deps) == S.COMPLETED
    assert deps.llm.primary.calls == 0
    [job] = await _get(factory, PipelineJob)
    assert job.outcome == JobOutcome.NOT_OPPORTUNITY.value


@pytest.mark.asyncio
async def test_extractor_says_not_an_opportunity(factory):
    job_id = await _ingest(factory, text="شوفوا الفيديو ده عن المذاكرة https://youtube.com/watch?v=abc")
    deps = make_deps([{"is_opportunity": False, "not_opportunity_reason": "a video, nothing to apply to"}])
    assert await run_job(factory, job_id, deps) == S.COMPLETED
    assert (await _get(factory, PipelineJob))[0].outcome == JobOutcome.NOT_OPPORTUNITY.value
    assert await _count(factory, Opportunity) == 0


@pytest.mark.asyncio
async def test_repost_of_known_opportunity_is_duplicate_without_llm_call(factory):
    first = await _ingest(factory)
    await run_job(factory, first, make_deps())
    repost = SAMPLE_OPPORTUNITY_TEXT.replace("🔥", "📢") + "\nمنقول"
    second = await _ingest(factory, text=repost, message_id="m2")
    deps = make_deps()
    assert await run_job(factory, second, deps) == S.COMPLETED
    assert deps.llm.primary.calls == 0  # same specific URL + no new date -> no AI call
    job = (await _get(factory, PipelineJob, id=second))[0]
    assert job.outcome == JobOutcome.DUPLICATE.value
    links = await _get(factory, OpportunitySourceMessage)
    assert sorted(link.relation for link in links) == ["duplicate", "origin"]
    assert await _count(factory, Opportunity) == 1


@pytest.mark.asyncio
async def test_extended_deadline_becomes_an_update_applied_only_after_approval(client, factory):
    first = await _ingest(factory)
    await run_job(factory, first, make_deps())
    [draft] = await _get(factory, AgentPostDraft)
    client.post(f"/api/v1/admin/agent/drafts/{draft.id}/approve", json={})
    await run_due_jobs(factory, make_deps())
    [opportunity] = await _get(factory, Opportunity)
    assert opportunity.review_status == OpportunityReviewStatus.PUBLISHED

    update_text = ("📢 تم مد آخر موعد التقديم لمنحة DAAD للماجستير 2027 حتى 30 نوفمبر 2026\n"
                   "رابط التقديم: https://www.daad.de/en/study-and-research-in-germany/scholarships/")
    second = await _ingest(factory, text=update_text, message_id="m-update")
    deps = make_deps([sample_extraction(deadline="2026-11-30", description=None, funding=None, eligibility=None)])
    assert await run_job(factory, second, deps) == S.WAITING_APPROVAL
    job = (await _get(factory, PipelineJob, id=second))[0]
    assert job.payload["dedup_outcome"] == "update_existing"
    assert job.payload["update_proposal"]["deadline"]["new"].startswith("2026-11-30")
    # Live data untouched until a human approves the update.
    [opportunity] = await _get(factory, Opportunity)
    assert opportunity.deadline.date() == dt.date(2026, 10, 15)
    assert opportunity.review_status == OpportunityReviewStatus.PUBLISHED

    new_draft = (await _get(factory, AgentPostDraft, job_id=second))[0]
    assert "30 نوفمبر 2026" in new_draft.body
    result = client.post(f"/api/v1/admin/agent/drafts/{new_draft.id}/approve", json={}).json()
    assert "deadline" in result["updated_fields"]
    [opportunity] = await _get(factory, Opportunity)
    assert opportunity.deadline.date() == dt.date(2026, 11, 30)
    history = await _get(factory, OpportunityChangeHistory)
    assert [h.field_name for h in history] == ["deadline"]


@pytest.mark.asyncio
async def test_human_reject_and_request_revision(client, factory):
    job_id = await _ingest(factory)
    await run_job(factory, job_id, make_deps())
    [draft] = await _get(factory, AgentPostDraft)
    revised = client.post(f"/api/v1/admin/agent/drafts/{draft.id}/request-revision", json={"notes": "اكتب الشروط بوضوح"})
    assert revised.status_code == 200
    assert await run_job(factory, job_id, make_deps()) == S.WAITING_APPROVAL
    drafts = sorted(await _get(factory, AgentPostDraft), key=lambda d: d.version)
    assert [d.version for d in drafts] == [1, 2] and drafts[0].status == DraftStatus.REVISION_REQUESTED.value

    rejected = client.post(f"/api/v1/admin/agent/drafts/{drafts[1].id}/reject",
                           json={"reason": "fake opportunity", "inaccurate_source": True})
    assert rejected.status_code == 200
    [opportunity] = await _get(factory, Opportunity)
    assert opportunity.review_status == OpportunityReviewStatus.REJECTED
    job = (await _get(factory, PipelineJob, id=job_id))[0]
    assert job.outcome == JobOutcome.REJECTED_BY_HUMAN.value
    [source] = await _get(factory, Source)
    assert source.trust_score < 0.4  # lowered by the human verdict
    assert await _count(factory, Publication) == 0


@pytest.mark.asyncio
async def test_verification_contradiction_parks_job_for_a_human_then_continue(client, factory):
    job_id = await _ingest(factory)
    deps = make_deps(fetcher=FakeFetcher(OFFICIAL_HTML.replace("15 October 2026", "1 October 2026")
                                         .replace("Extended deadline: 30 November 2026", "")))
    assert await run_job(factory, job_id, deps) == S.NEEDS_REVIEW
    job = (await _get(factory, PipelineJob, id=job_id))[0]
    assert job.payload["verification"]["level"] == "contradicted"
    assert await _count(factory, AgentPostDraft) == 0
    # A human checks and lets it continue; it still needs approval afterwards.
    assert client.post(f"/api/v1/admin/agent/jobs/{job_id}/continue", json={"notes": "page outdated"}).status_code == 200
    assert await run_job(factory, job_id, deps) == S.WAITING_APPROVAL


@pytest.mark.asyncio
async def test_expired_opportunity_is_closed_without_draft(factory):
    job_id = await _ingest(factory)
    deps = make_deps()
    deps.today = lambda: dt.date(2026, 12, 1)
    assert await run_job(factory, job_id, deps) == S.COMPLETED
    assert (await _get(factory, PipelineJob))[0].outcome == JobOutcome.EXPIRED.value
    assert await _count(factory, AgentPostDraft) == 0


# ==========================================================================
# Failures, retries, dead letter, restart recovery
# ==========================================================================

@pytest.mark.asyncio
async def test_llm_rate_limit_retries_then_dead_letters(factory, monkeypatch):
    job_id = await _ingest(factory)
    deps = make_deps([RateLimitError("429", code="LLM_RATE_LIMITED")])
    async with factory() as db:
        job = await db.get(PipelineJob, job_id)
        job.max_attempts = 3
        await db.commit()
    assert await run_job(factory, job_id, deps) == S.EXTRACTING  # retry scheduled at the failing stage
    job = (await _get(factory, PipelineJob, id=job_id))[0]
    assert job.attempts == 1 and job.next_retry_at is not None and job.last_error_category == "rate_limit"
    await run_job(factory, job_id, deps)
    assert await run_job(factory, job_id, deps) == S.DEAD_LETTER
    assert deps.llm.primary.calls == 3  # bounded, never infinite
    metrics_job = (await _get(factory, PipelineJob, id=job_id))[0]
    assert metrics_job.status == S.DEAD_LETTER.value


@pytest.mark.asyncio
async def test_schema_failure_goes_to_needs_review(factory):
    job_id = await _ingest(factory)
    deps = make_deps(["not json at all"])
    assert await run_job(factory, job_id, deps) == S.NEEDS_REVIEW
    job = (await _get(factory, PipelineJob, id=job_id))[0]
    assert job.last_error_code == "LLM_SCHEMA_VALIDATION_FAILED"
    assert deps.llm.primary.calls == 2  # 1 + max_validation_retries(1)
    assert await _count(factory, Opportunity) == 0  # the half-finished stage was rolled back


@pytest.mark.asyncio
async def test_restart_recovery_resumes_without_repeating_the_llm_call(factory):
    job_id = await _ingest(factory)
    deps = make_deps()

    class Crash(Exception):
        pass

    # Crash right after extraction was committed (inside verification).
    class CrashingFetcher:
        async def fetch(self, url):
            raise Crash("worker killed")

    t0 = dt.datetime.now(dt.timezone.utc)
    async with factory() as db:
        [job] = await jobs.claim_due_jobs(db, owner="worker-1", now=t0, lease_seconds=60)
        await db.commit()
    crashing = make_deps(fetcher=CrashingFetcher())
    crashing.llm = deps.llm
    from app.agent.pipeline import advance_job

    async with factory() as db:
        job = await db.get(PipelineJob, job_id)
        with pytest.raises(Crash):
            await advance_job(db, job, crashing)
        # process dies: no rollback handling, no lease release
    job = (await _get(factory, PipelineJob, id=job_id))[0]
    assert job.status == S.VERIFYING.value and job.locked_by == "worker-1"
    assert deps.llm.primary.calls == 1

    # Before the (renewed) lease expires nobody may take the job...
    async with factory() as db:
        assert await jobs.claim_due_jobs(db, owner="worker-2", now=t0 + dt.timedelta(seconds=120)) == []
    # ...after it expired, another worker recovers it.
    async with factory() as db:
        claimed = await jobs.claim_due_jobs(db, owner="worker-2",
                                            now=t0 + dt.timedelta(seconds=settings.agent_job_lease_seconds + 120))
        await db.commit()
    assert [j.id for j in claimed] == [job_id]
    assert await run_job(factory, job_id, deps) == S.WAITING_APPROVAL
    assert deps.llm.primary.calls == 1  # extraction was checkpointed, not repeated
    assert await _count(factory, Opportunity) == 1


# ==========================================================================
# Publishing: idempotency, failures, unknown delivery, Telegram adapter
# ==========================================================================

async def _approved_draft(client, factory):
    job_id = await _ingest(factory)
    await run_job(factory, job_id, make_deps())
    [draft] = await _get(factory, AgentPostDraft)
    assert client.post(f"/api/v1/admin/agent/drafts/{draft.id}/approve", json={}).status_code == 200
    return job_id, draft


@pytest.mark.asyncio
async def test_publisher_failure_is_retried_then_succeeds_once(client, factory):
    job_id, _ = await _approved_draft(client, factory)
    publisher = MockPublisher(fail_times=1)
    deps = make_deps(publishers=[publisher])
    assert await run_job(factory, job_id, deps) == S.PUBLISHING  # failed, retry scheduled
    [publication] = await _get(factory, Publication)
    assert publication.status == PublicationStatus.FAILED.value and publication.attempts == 1
    async with factory() as db:
        job = await db.get(PipelineJob, job_id)
        job.next_retry_at = None
        await db.commit()
    assert await run_job(factory, job_id, deps) == S.COMPLETED
    [publication] = await _get(factory, Publication)
    assert publication.status == PublicationStatus.PUBLISHED.value and publication.attempts == 2
    assert len(publisher.sent) == 1


@pytest.mark.asyncio
async def test_uncertain_delivery_is_never_resent_automatically(client, factory):
    job_id, _ = await _approved_draft(client, factory)
    publisher = MockPublisher(fail_times=1, uncertain=True)
    deps = make_deps(publishers=[publisher])
    assert await run_job(factory, job_id, deps) == S.NEEDS_REVIEW
    [publication] = await _get(factory, Publication)
    assert publication.status == PublicationStatus.UNKNOWN.value
    assert publisher.sent == []
    # A human confirms it actually went out.
    confirm = client.post(f"/api/v1/admin/agent/publications/{publication.id}/confirm",
                          json={"external_url": "https://t.me/apex/1"})
    assert confirm.status_code == 200 and confirm.json()["status"] == "published"


@pytest.mark.asyncio
async def test_duplicate_publication_is_impossible(client, factory):
    _, draft = await _approved_draft(client, factory)
    publisher = MockPublisher()
    async with factory() as db:
        d = await db.get(AgentPostDraft, draft.id)
        first = await publish_draft(db, d, publisher, title="t", link=None, commit=db.commit)
        second = await publish_draft(db, d, publisher, title="t", link=None, commit=db.commit)
    assert first.sent_now and not second.sent_now
    assert first.publication.id == second.publication.id
    assert len(publisher.sent) == 1
    assert await _count(factory, Publication) == 1


@pytest.mark.asyncio
async def test_crash_mid_publish_is_reconciled_not_resent(client, factory):
    _, draft = await _approved_draft(client, factory)
    publisher = MockPublisher()
    async with factory() as db:
        d = await db.get(AgentPostDraft, draft.id)
        outcome = await publish_draft(db, d, publisher, title="t", link=None, commit=db.commit)
        # Simulate: delivered, but the process died before recording PUBLISHED.
        outcome.publication.status = PublicationStatus.PUBLISHING.value
        await db.commit()
        again = await publish_draft(db, d, publisher, title="t", link=None, commit=db.commit)
    assert not again.sent_now and again.publication.status == PublicationStatus.PUBLISHED.value
    assert len(publisher.sent) == 1


@pytest.mark.asyncio
async def test_manual_publisher_waits_for_confirmation(client, factory):
    from app.agent.publishing import ManualPublisher

    job_id, draft = await _approved_draft(client, factory)
    assert await run_job(factory, job_id, make_deps(publishers=[ManualPublisher()])) == S.COMPLETED
    [publication] = await _get(factory, Publication)
    assert publication.status == PublicationStatus.AWAITING_MANUAL.value
    assert (await _get(factory, AgentPostDraft))[0].status == DraftStatus.APPROVED.value
    client.post(f"/api/v1/admin/agent/publications/{publication.id}/confirm", json={"external_url": "https://chat.whatsapp.com/x"})
    assert (await _get(factory, AgentPostDraft))[0].status == DraftStatus.PUBLISHED.value


@pytest.mark.asyncio
async def test_telegram_publisher_adapter():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 77, "chat": {"username": "apexops"}}})

    publisher = TelegramBotPublisher(token="123456:ABC", chat_id="@apexops", transport=httpx.MockTransport(handler))
    result = await publisher.publish(PublishPayload("k", uuid.uuid4(), uuid.uuid4(), "t", "body text", None, "@apexops"))
    assert result.external_url == "https://t.me/apexops/77"
    assert seen["url"].endswith("/sendMessage") and seen["body"]["chat_id"] == "@apexops"

    failing = TelegramBotPublisher(token="1:A", chat_id="x", transport=httpx.MockTransport(
        lambda r: httpx.Response(403, json={"ok": False, "description": "bot is not a member"})))
    with pytest.raises(Exception) as info:
        await failing.publish(PublishPayload("k", uuid.uuid4(), uuid.uuid4(), "t", "b", None, "x"))
    assert info.value.delivered is False


# ==========================================================================
# Volunteer submissions + admin endpoints
# ==========================================================================

@pytest.mark.asyncio
async def test_volunteer_submission_and_dead_letter_requeue(client, factory):
    response = client.post("/api/v1/agent/submissions", json={"text": SAMPLE_OPPORTUNITY_TEXT, "note": "من صفحة الجامعة"})
    assert response.status_code == 202 and response.json()["status"] == "queued"
    job_id = uuid.UUID(response.json()["job_id"])
    [source] = await _get(factory, Source)
    assert source.source_type == "volunteer" and source.access_status == "approved"

    async with factory() as db:
        job = await db.get(PipelineJob, job_id)
        job.max_attempts = 1
        await db.commit()
    assert await run_job(factory, job_id, make_deps([RateLimitError("429")])) == S.DEAD_LETTER
    listed = client.get("/api/v1/admin/agent/jobs", params={"status": "dead_letter"}).json()
    assert [j["id"] for j in listed] == [str(job_id)]
    assert client.post(f"/api/v1/admin/agent/jobs/{job_id}/requeue").json()["job_status"] == "queued"
    assert await run_job(factory, job_id, make_deps()) == S.WAITING_APPROVAL


def test_admin_endpoints_require_moderator(factory):
    async def _db():
        async with factory() as session:
            yield session

    student = User(id=uuid.uuid4(), email="s@apex.test", is_active=True, is_superuser=False, admin_role=None)
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: student
    try:
        with TestClient(app) as c:
            assert c.get("/api/v1/admin/agent/approvals").status_code == 403
            assert c.post("/api/v1/agent/submissions", json={"text": "x" * 20}).status_code == 403
            assert c.patch(f"/api/v1/admin/agent/sources/{uuid.uuid4()}", json={"trust_score": 1}).status_code == 403
    finally:
        app.dependency_overrides.clear()
