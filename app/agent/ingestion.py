"""Unified ingestion.

Every connector (WhatsApp, volunteer form, Telegram, website, RSS) produces
the same `IngestionEvent`. `ingest_event` is the ONE entry point into the
pipeline, so allow-listing, loop protection, idempotency and exact
duplicate detection are implemented once:

1. resolve (or create) the `Source`; refuse sources that are not APPROVED
2. drop the bot's own messages (loop protection, defense in depth -- the
   connector already filters them)
3. reject malformed events (no text, no links, no attachments)
4. idempotency: the same platform message id from the same source is
   stored once (DB unique constraint + pre-check)
5. normalize (links, normalized content, content hash)
6. exact duplicate content (same hash already seen) -> stored for
   provenance, linked to the known opportunity, NO new job
7. otherwise store the `SourceMessage` and enqueue a `PipelineJob` whose
   priority comes from the source's trust score
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum
from typing import Any

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import jobs
from app.agent.normalization import content_hash, extract_links, normalize_content
from app.agent.observability import log_stage
from app.agent.reputation import default_trust_for, job_priority
from app.models.agent import OpportunitySourceMessage, PipelineJob, SourceMessage
from app.models.agent_enums import SourceAccessStatus, SourceMessageStatus
from app.models.enums import SourceType
from app.models.source import Source

MAX_TEXT_LENGTH = 20_000
MAX_LINKS = 25
MAX_ATTACHMENTS = 10


class AttachmentMeta(BaseModel):
    """Metadata only -- the agent never downloads or stores media bytes."""

    kind: str = Field(max_length=30, description="image | document | video | audio | other")
    mime_type: str | None = Field(default=None, max_length=120)
    file_name: str | None = Field(default=None, max_length=255)
    size_bytes: int | None = Field(default=None, ge=0)
    caption: str | None = Field(default=None, max_length=2000)


class IngestionEvent(BaseModel):
    """The single internal event every connector produces."""

    source_type: SourceType
    # Stable key of the feed inside its platform (group id, channel handle,
    # volunteer id, feed URL). Together with `source_type` it identifies the
    # `Source` row: name = "<source_type>:<source_key>".
    source_key: str = Field(min_length=1, max_length=200)
    source_display_name: str | None = Field(default=None, max_length=255)
    source_base_url: str = Field(default="", max_length=2048)

    external_message_id: str | None = Field(default=None, max_length=255)
    sender_id: str | None = Field(default=None, max_length=255)
    sender_name: str | None = Field(default=None, max_length=255)
    group_id: str | None = Field(default=None, max_length=255)
    group_name: str | None = Field(default=None, max_length=255)
    text: str = Field(default="")
    links: list[str] = Field(default_factory=list)
    attachments: list[AttachmentMeta] = Field(default_factory=list)
    received_at: dt.datetime | None = None
    from_bot: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("text")
    @classmethod
    def _cap_text(cls, value: str) -> str:
        return (value or "")[:MAX_TEXT_LENGTH]

    @field_validator("links")
    @classmethod
    def _cap_links(cls, value: list[str]) -> list[str]:
        return [v.strip() for v in value if v and v.strip()][:MAX_LINKS]

    @field_validator("attachments")
    @classmethod
    def _cap_attachments(cls, value: list[AttachmentMeta]) -> list[AttachmentMeta]:
        return value[:MAX_ATTACHMENTS]


class IngestStatus(str, enum.Enum):
    QUEUED = "queued"
    DUPLICATE_DELIVERY = "duplicate_delivery"  # same platform message id already stored
    DUPLICATE_CONTENT = "duplicate_content"  # same normalized content already seen
    IGNORED_SELF = "ignored_self"  # the bot's own message
    REJECTED_SOURCE = "rejected_source"  # source not approved / inactive
    REJECTED_MALFORMED = "rejected_malformed"


@dataclasses.dataclass
class IngestResult:
    status: IngestStatus
    source: Source | None = None
    source_message: SourceMessage | None = None
    job: PipelineJob | None = None
    reason: str | None = None

    @property
    def accepted(self) -> bool:
        return self.status == IngestStatus.QUEUED


def source_name_for(source_type: SourceType | str, source_key: str) -> str:
    value = source_type.value if isinstance(source_type, SourceType) else str(source_type)
    return f"{value}:{source_key}"[:255]


async def resolve_source(
    db: AsyncSession, event: IngestionEvent, *, auto_approve: bool
) -> Source:
    """Get-or-create the `Source` for an event. A NEW source is created
    APPROVED only when the caller already proved access (e.g. its group id
    is on the allowlist); otherwise PENDING, which refuses ingestion until
    an admin approves it."""
    name = source_name_for(event.source_type, event.source_key)
    existing = (await db.execute(select(Source).where(Source.name == name))).scalars().first()
    if existing is not None:
        return existing
    source = Source(
        name=name,
        base_url=event.source_base_url or "",
        source_type=event.source_type.value,
        active_status=True,
        trust_score=default_trust_for(event.source_type.value),
        access_status=(SourceAccessStatus.APPROVED if auto_approve else SourceAccessStatus.PENDING).value,
    )
    db.add(source)
    await db.flush()
    log_stage("source_created", source_id=source.id, source_type=source.source_type, status=source.access_status)
    return source


async def ingest_event(
    db: AsyncSession, event: IngestionEvent, *, auto_approve_source: bool = False
) -> IngestResult:
    """Store `event` and enqueue it, or explain why not. Does not commit."""
    if event.from_bot:
        log_stage("ingest_ignored_self", source_type=event.source_type.value)
        return IngestResult(IngestStatus.IGNORED_SELF, reason="message was sent by the APEX bot itself")

    caption_text = "\n".join(a.caption for a in event.attachments if a.caption)
    full_text = event.text if not caption_text else f"{event.text}\n{caption_text}".strip()
    links = list(dict.fromkeys([*event.links, *extract_links(full_text)]))[:MAX_LINKS]
    if not event.text.strip() and not links and not event.attachments:
        return IngestResult(IngestStatus.REJECTED_MALFORMED, reason="event has no text, links or attachments")

    source = await resolve_source(db, event, auto_approve=auto_approve_source)
    if source.access_status != SourceAccessStatus.APPROVED.value or not source.active_status:
        log_stage("ingest_rejected_source", source_id=source.id, status=source.access_status)
        return IngestResult(IngestStatus.REJECTED_SOURCE, source=source, reason=f"source access is {source.access_status}")

    if event.external_message_id:
        existing = (
            await db.execute(
                select(SourceMessage).where(
                    SourceMessage.source_id == source.id,
                    SourceMessage.external_message_id == event.external_message_id,
                )
            )
        ).scalars().first()
        if existing is not None:
            log_stage("ingest_duplicate_delivery", source_id=source.id, source_message_id=existing.id)
            return IngestResult(IngestStatus.DUPLICATE_DELIVERY, source=source, source_message=existing)

    digest = content_hash(full_text)
    message = SourceMessage(
        source_id=source.id,
        source_type=source.source_type,
        external_message_id=event.external_message_id,
        sender_id=event.sender_id,
        sender_name=event.sender_name,
        group_id=event.group_id,
        group_name=event.group_name,
        raw_text=full_text,
        links=links,
        attachment_metadata=[a.model_dump() for a in event.attachments] or None,
        normalized_content=normalize_content(full_text),
        content_hash=digest,
        received_at=event.received_at or jobs.utcnow(),
        processing_status=SourceMessageStatus.RECEIVED.value,
    )

    earlier: SourceMessage | None = None
    if digest:
        earlier = (
            await db.execute(
                select(SourceMessage)
                .where(SourceMessage.content_hash == digest)
                .order_by(SourceMessage.created_at.asc())
                .limit(1)
            )
        ).scalars().first()

    try:
        async with db.begin_nested():
            db.add(message)
            await db.flush()
    except IntegrityError:
        # Concurrent delivery of the same platform message: the other insert won.
        existing = (
            await db.execute(
                select(SourceMessage).where(
                    SourceMessage.source_id == source.id,
                    SourceMessage.external_message_id == event.external_message_id,
                )
            )
        ).scalars().first()
        return IngestResult(IngestStatus.DUPLICATE_DELIVERY, source=source, source_message=existing)

    if earlier is not None:
        message.processing_status = SourceMessageStatus.DUPLICATE_MESSAGE.value
        message.dedup_outcome = "duplicate"
        message.dedup_details = {"layer": "message_content_hash", "earlier_source_message_id": str(earlier.id)}
        message.opportunity_id = earlier.opportunity_id
        if earlier.opportunity_id is not None:
            db.add(
                OpportunitySourceMessage(
                    opportunity_id=earlier.opportunity_id, source_message_id=message.id, relation="duplicate"
                )
            )
        await db.flush()
        log_stage(
            "ingest_duplicate_content",
            source_id=source.id,
            source_message_id=message.id,
            opportunity_id=earlier.opportunity_id,
        )
        return IngestResult(IngestStatus.DUPLICATE_CONTENT, source=source, source_message=message)

    message.processing_status = SourceMessageStatus.QUEUED.value
    job = await jobs.create_job(db, source_message_id=message.id, priority=job_priority(source.trust_score))
    log_stage(
        "ingest_queued",
        job_id=job.id,
        correlation_id=job.correlation_id,
        source_id=source.id,
        source_message_id=message.id,
        source_type=source.source_type,
        links=len(links),
    )
    return IngestResult(IngestStatus.QUEUED, source=source, source_message=message, job=job)
